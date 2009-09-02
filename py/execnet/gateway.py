import sys, os, inspect, socket, atexit, weakref
import py
from subprocess import Popen, PIPE
from py.__.execnet.gateway_base import BaseGateway, Message, Popen2IO, SocketIO
from py.__.execnet.gateway_base import ExecnetAPI

# the list of modules that must be send to the other side 
# for bootstrapping gateways
# XXX we'd like to have a leaner and meaner bootstrap mechanism 

startup_modules = [
    'py.__.execnet.gateway_base', 
]

debug = False

def getsource(dottedname): 
    mod = __import__(dottedname, None, None, ['__doc__'])
    return inspect.getsource(mod) 

class GatewayCleanup:
    def __init__(self): 
        self._activegateways = weakref.WeakKeyDictionary()
        atexit.register(self.cleanup_atexit)

    def register(self, gateway):
        assert gateway not in self._activegateways
        self._activegateways[gateway] = True

    def unregister(self, gateway):
        del self._activegateways[gateway]

    def cleanup_atexit(self):
        if debug:
            debug.writeslines(["="*20, "cleaning up", "=" * 20])
            debug.flush()
        for gw in list(self._activegateways):
            gw.exit()
            #gw.join() # should work as well

    
class InitiatingGateway(BaseGateway):
    """ initialize gateways on both sides of a inputoutput object. """
    _cleanup = GatewayCleanup()

    def __init__(self, io):
        self._remote_bootstrap_gateway(io)
        super(InitiatingGateway, self).__init__(io=io, _startcount=1) 
        # XXX we dissallow execution form the other side
        self._initreceive()
        self.hook = py._com.HookRelay(ExecnetAPI, py._com.comregistry)
        self.hook.pyexecnet_gateway_init(gateway=self)
        self._cleanup.register(self) 

    def __repr__(self):
        """ return string representing gateway type and status. """
        if hasattr(self, 'remoteaddress'):
            addr = '[%s]' % (self.remoteaddress,)
        else:
            addr = ''
        try:
            r = (self._receiverthread.isAlive() and "receiving" or 
                 "not receiving")
            s = "sending" # XXX
            i = len(self._channelfactory.channels())
        except AttributeError:
            r = s = "uninitialized"
            i = "no"
        return "<%s%s %s/%s (%s active channels)>" %(
                self.__class__.__name__, addr, r, s, i)

    def exit(self):
        """ Try to stop all exec and IO activity. """
        self._cleanup.unregister(self)
        self._stopexec()
        self._stopsend()
        self.hook.pyexecnet_gateway_exit(gateway=self)

    def _remote_bootstrap_gateway(self, io, extra=''):
        """ return Gateway with a asynchronously remotely
            initialized counterpart Gateway (which may or may not succeed).
            Note that the other sides gateways starts enumerating
            its channels with even numbers while the sender
            gateway starts with odd numbers.  This allows to
            uniquely identify channels across both sides.
        """
        bootstrap = [extra]
        bootstrap += [getsource(x) for x in startup_modules]
        bootstrap += [io.server_stmt, 
                      "BaseGateway(io=io, _startcount=2)._servemain()", 
                     ]
        source = "\n".join(bootstrap)
        self._trace("sending gateway bootstrap code")
        #open("/tmp/bootstrap.py", 'w').write(source)
        repr_source = repr(source) + "\n"
        io.write(repr_source.encode('ascii'))

    def _rinfo(self, update=False):
        """ return some sys/env information from remote. """
        if update or not hasattr(self, '_cache_rinfo'):
            ch = self.remote_exec(rinfo_source)
            self._cache_rinfo = RInfo(**ch.receive())
        return self._cache_rinfo

    def remote_exec(self, source): 
        """ return channel object and connect it to a remote
            execution thread where the given 'source' executes
            and has the sister 'channel' object in its global 
            namespace.
        """
        source = str(py.code.Source(source))
        channel = self.newchannel() 
        self._send(Message.CHANNEL_OPEN(channel.id, source))
        return channel 

    def remote_init_threads(self, num=None):
        """ start up to 'num' threads for subsequent 
            remote_exec() invocations to allow concurrent
            execution. 
        """
        if hasattr(self, '_remotechannelthread'):
            raise IOError("remote threads already running")
        from py.__.thread import pool
        source = py.code.Source(pool, """
            execpool = WorkerPool(maxthreads=%r)
            gw = channel.gateway
            while 1:
                task = gw._execqueue.get()
                if task is None:
                    gw._stopsend()
                    execpool.shutdown()
                    execpool.join()
                    raise gw._StopExecLoop
                execpool.dispatch(gw._executetask, task)
        """ % num)
        self._remotechannelthread = self.remote_exec(source)

    def _remote_redirect(self, stdout=None, stderr=None): 
        """ return a handle representing a redirection of a remote 
            end's stdout to a local file object.  with handle.close() 
            the redirection will be reverted.   
        """ 
        # XXX implement a remote_exec_in_globals(...)
        #     to send ThreadOut implementation over 
        clist = []
        for name, out in ('stdout', stdout), ('stderr', stderr): 
            if out: 
                outchannel = self.newchannel()
                outchannel.setcallback(getattr(out, 'write', out))
                channel = self.remote_exec(""" 
                    import sys
                    outchannel = channel.receive() 
                    ThreadOut(sys, %r).setdefaultwriter(outchannel.send)
                """ % name) 
                channel.send(outchannel)
                clist.append(channel)
        for c in clist: 
            c.waitclose() 
        class Handle: 
            def close(_): 
                for name, out in ('stdout', stdout), ('stderr', stderr): 
                    if out: 
                        c = self.remote_exec("""
                            import sys
                            channel.gateway._ThreadOut(sys, %r).resetdefault()
                        """ % name) 
                        c.waitclose() 
        return Handle()



class RInfo:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
    def __repr__(self):
        info = ", ".join(["%s=%s" % item 
                for item in self.__dict__.items()])
        return "<RInfo %r>" % info

rinfo_source = """
import sys, os
channel.send(dict(
    executable = sys.executable, 
    version_info = tuple([sys.version_info[i] for i in range(5)]),
    platform = sys.platform,
    cwd = os.getcwd(),
    pid = os.getpid(),
))
"""

class PopenCmdGateway(InitiatingGateway):
    def __init__(self, cmd):
        # on win close_fds=True does not work, not sure it'd needed
        #p = Popen(cmd, shell=True, stdin=PIPE, stdout=PIPE, close_fds=True)
        self._popen = p = Popen(cmd, shell=True, stdin=PIPE, stdout=PIPE) 
        self._cmd = cmd
        io = Popen2IO(p.stdin, p.stdout)
        super(PopenCmdGateway, self).__init__(io=io)

    def exit(self):
        super(PopenCmdGateway, self).exit()
        self._popen.poll()

class PopenGateway(PopenCmdGateway):
    """ This Gateway provides interaction with a newly started
        python subprocess. 
    """
    def __init__(self, python=None):
        """ instantiate a gateway to a subprocess 
            started with the given 'python' executable. 
        """
        if not python:
            python = sys.executable
        cmd = ('%s -u -c "import sys ; '
               'exec(eval(sys.stdin.readline()))"' % python)
        super(PopenGateway, self).__init__(cmd)

    def _remote_bootstrap_gateway(self, io, extra=''):
        # have the subprocess use the same PYTHONPATH and py lib 
        x = py.path.local(py.__file__).dirpath().dirpath()
        ppath = os.environ.get('PYTHONPATH', '')
        plist = [str(x)] + ppath.split(':')
        s = "\n".join([extra, 
            "import sys ; sys.path[:0] = %r" % (plist,), 
            "import os ; os.environ['PYTHONPATH'] = %r" % ppath, 
            str(py.code.Source(stdouterrin_setnull)), 
            "stdouterrin_setnull()", 
            ""
            ])
        super(PopenGateway, self)._remote_bootstrap_gateway(io, s)

class SocketGateway(InitiatingGateway):
    """ This Gateway provides interaction with a remote process
        by connecting to a specified socket.  On the remote
        side you need to manually start a small script 
        (py/execnet/script/socketserver.py) that accepts
        SocketGateway connections. 
    """
    def __init__(self, host, port):
        """ instantiate a gateway to a process accessed
            via a host/port specified socket. 
        """
        self.host = host = str(host)
        self.port = port = int(port)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((host, port))
        io = SocketIO(sock)
        super(SocketGateway, self).__init__(io=io)
        self.remoteaddress = '%s:%d' % (self.host, self.port)

    def new_remote(cls, gateway, hostport=None): 
        """ return a new (connected) socket gateway, instatiated
            indirectly through the given 'gateway'. 
        """ 
        if hostport is None: 
            host, port = ('', 0)  # XXX works on all platforms? 
        else:   
            host, port = hostport 
        mydir = py.path.local(__file__).dirpath()
        socketserverbootstrap = py.code.Source(
            mydir.join('script', 'socketserver.py').read('r'), """
            import socket
            sock = bind_and_listen((%r, %r)) 
            port = sock.getsockname()
            channel.send(port) 
            startserver(sock)
            """ % (host, port)
        ) 
        # execute the above socketserverbootstrap on the other side
        channel = gateway.remote_exec(socketserverbootstrap)
        (realhost, realport) = channel.receive()
        #gateway._trace("new_remote received" 
        #               "port=%r, hostname = %r" %(realport, hostname))
        return py.execnet.SocketGateway(host, realport) 
    new_remote = classmethod(new_remote)

    
class SshGateway(PopenCmdGateway):
    """ This Gateway provides interaction with a remote Python process,
        established via the 'ssh' command line binary.  
        The remote side needs to have a Python interpreter executable. 
    """
    def __init__(self, sshaddress, remotepython=None, 
        identity=None, ssh_config=None): 
        """ instantiate a remote ssh process with the 
            given 'sshaddress' and remotepython version.
            you may specify an ssh_config file. 
            DEPRECATED: you may specify an 'identity' filepath. 
        """
        self.remoteaddress = sshaddress
        if remotepython is None:
            remotepython = "python"
        remotecmd = '%s -u -c "exec input()"' % (remotepython,)
        cmdline = [sshaddress, remotecmd]
        # XXX Unix style quoting
        for i in range(len(cmdline)):
            cmdline[i] = "'" + cmdline[i].replace("'", "'\\''") + "'"
        cmd = 'ssh -C'
        if identity is not None: 
            py.log._apiwarn("1.0", "pass in 'ssh_config' file instead of identity")
            cmd += ' -i %s' % (identity,)
        if ssh_config is not None:
            cmd += ' -F %s' % (ssh_config)
        cmdline.insert(0, cmd) 
        cmd = ' '.join(cmdline)
        super(SshGateway, self).__init__(cmd)
       
    def _remote_bootstrap_gateway(self, io, s=""): 
        extra = "\n".join([
            str(py.code.Source(stdouterrin_setnull)), 
            "stdouterrin_setnull()",
            s, 
        ])
        super(SshGateway, self)._remote_bootstrap_gateway(io, extra)


def stdouterrin_setnull():
    """ redirect file descriptors 0 and 1 (and possibly 2) to /dev/null. 
        note that this function may run remotely without py lib support. 
    """
    # complete confusion (this is independent from the sys.stdout
    # and sys.stderr redirection that gateway.remote_exec() can do)
    # note that we redirect fd 2 on win too, since for some reason that
    # blocks there, while it works (sending to stderr if possible else
    # ignoring) on *nix
    import sys, os
    try:
        devnull = os.devnull
    except AttributeError:
        if os.name == 'nt':
            devnull = 'NUL'
        else:
            devnull = '/dev/null'
    # stdin
    sys.stdin  = os.fdopen(os.dup(0), 'r', 1)
    fd = os.open(devnull, os.O_RDONLY)
    os.dup2(fd, 0)
    os.close(fd)

    # stdout
    sys.stdout = os.fdopen(os.dup(1), 'w', 1)
    fd = os.open(devnull, os.O_WRONLY)
    os.dup2(fd, 1)

    # stderr for win32
    if os.name == 'nt':
        sys.stderr = os.fdopen(os.dup(2), 'w', 1)
        os.dup2(fd, 2)
    os.close(fd)
