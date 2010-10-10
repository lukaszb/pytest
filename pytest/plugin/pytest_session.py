""" basic test session implementation.

* drives collection of tests
* triggers executions of tests
* produces events used by reporting
"""

import py
import pytest
import os, sys

def pytest_addoption(parser):
    group = parser.getgroup("general", "running and selection options")
    group._addoption('-x', '--exitfirst', action="store_true", default=False,
               dest="exitfirst",
               help="exit instantly on first error or failed test."),
    group._addoption('--maxfail', metavar="num",
               action="store", type="int", dest="maxfail", default=0,
               help="exit after first num failures or errors.")

    group = parser.getgroup("collect", "collection")
    group.addoption('--collectonly',
        action="store_true", dest="collectonly",
        help="only collect tests, don't execute them."),
    group.addoption("--ignore", action="append", metavar="path",
        help="ignore path during collection (multi-allowed).")
    group.addoption('--confcutdir', dest="confcutdir", default=None,
        metavar="dir",
        help="only load conftest.py's relative to specified dir.")

    group = parser.getgroup("debugconfig",
        "test process debugging and configuration")
    group.addoption('--basetemp', dest="basetemp", default=None, metavar="dir",
               help="base temporary directory for this test run.")

def pytest_configure(config):
    # compat
    if config.getvalue("exitfirst"):
        config.option.maxfail = 1


def pytest_cmdline_main(config):
    return Session(config).main()

def pytest_perform_collection(session):
    collection = session.collection
    assert not hasattr(collection, 'items')
    hook = session.config.hook
    collection.items = items = collection.perform_collect()
    hook.pytest_collection_modifyitems(config=session.config, items=items)
    hook.pytest_log_finishcollection(collection=collection)
    return True

def pytest_runtest_mainloop(session):
    if session.config.option.collectonly:
        return True
    for item in session.collection.items:
        item.config.hook.pytest_runtest_protocol(item=item)
        if session.shouldstop:
            raise session.Interrupted(session.shouldstop)
    return True

def pytest_ignore_collect(path, config):
    p = path.dirpath()
    ignore_paths = config.getconftest_pathlist("collect_ignore", path=p)
    ignore_paths = ignore_paths or []
    excludeopt = config.getvalue("ignore")
    if excludeopt:
        ignore_paths.extend([py.path.local(x) for x in excludeopt])
    return path in ignore_paths

def pytest_collect_directory(path, parent):
    if not parent.recfilter(path): # by default special ".cvs", ...
        # check if cmdline specified this dir or a subdir directly
        for arg in parent.collection._argfspaths:
            if path == arg or arg.relto(path):
                break
        else:
            return
    return parent.Directory(path, parent=parent)

def pytest_report_iteminfo(item):
    return item.reportinfo()


# exitcodes for the command line
EXIT_OK = 0
EXIT_TESTSFAILED = 1
EXIT_INTERRUPTED = 2
EXIT_INTERNALERROR = 3
EXIT_NOHOSTS = 4

class Session(object):
    nodeid = ""
    class Interrupted(KeyboardInterrupt):
        """ signals an interrupted test run. """
        __module__ = 'builtins' # for py3

    def __init__(self, config):
        self.config = config
        self.config.pluginmanager.register(self, name="session", prepend=True)
        self._testsfailed = 0
        self.shouldstop = False
        self.collection = Collection(config) # XXX move elswehre

    def pytest_runtest_logreport(self, report):
        if report.failed:
            self._testsfailed += 1
            maxfail = self.config.getvalue("maxfail")
            if maxfail and self._testsfailed >= maxfail:
                self.shouldstop = "stopping after %d failures" % (
                    self._testsfailed)
                self.collection.shouldstop = self.shouldstop
    pytest_collectreport = pytest_runtest_logreport

    def main(self):
        """ main loop for running tests. """
        self.shouldstop = False
        self.exitstatus = EXIT_OK
        config = self.config
        try:
            config.pluginmanager.do_configure(config)
            config.hook.pytest_sessionstart(session=self)
            config.hook.pytest_perform_collection(session=self)
            config.hook.pytest_runtest_mainloop(session=self)
        except self.config.Error:
            raise
        except KeyboardInterrupt:
            excinfo = py.code.ExceptionInfo()
            self.config.hook.pytest_keyboard_interrupt(excinfo=excinfo)
            self.exitstatus = EXIT_INTERRUPTED
        except:
            excinfo = py.code.ExceptionInfo()
            self.config.pluginmanager.notify_exception(excinfo)
            self.exitstatus = EXIT_INTERNALERROR
            if excinfo.errisinstance(SystemExit):
                sys.stderr.write("mainloop: caught Spurious SystemExit!\n")

        if not self.exitstatus and self._testsfailed:
            self.exitstatus = EXIT_TESTSFAILED
        self.config.hook.pytest_sessionfinish(
            session=self, exitstatus=self.exitstatus,
        )
        config.pluginmanager.do_unconfigure(config)
        return self.exitstatus

class Collection:
    def __init__(self, config):
        self.config = config
        self.topdir = gettopdir(self.config.args)
        self._argfspaths = [py.path.local(decodearg(x)[0])
                                     for x in self.config.args]
        x = pytest.collect.Directory(fspath=self.topdir,
            config=config, collection=self)
        self._topcollector = x.consider_dir(self.topdir)
        self._topcollector.parent = None

    def _normalizearg(self, arg):
        return "::".join(self._parsearg(arg))

    def _parsearg(self, arg, base=None):
        """ return normalized name list for a command line specified id
        which might be of the form x/y/z::name1::name2
        and should result into the form x::y::z::name1::name2
        """
        if base is None:
            base = py.path.local()
        parts = str(arg).split("::")
        path = base.join(parts[0], abs=True)
        if not path.check():
            raise self.config.Error("file not found: %s" %(path,))
        topdir = self.topdir
        if path != topdir and not path.relto(topdir):
            raise self.config.Error("path %r is not relative to %r" %
                (str(path), str(topdir)))
        topparts = path.relto(topdir).split(path.sep)
        return topparts + parts[1:]

    def getid(self, node):
        """ return id for node, relative to topdir. """
        path = node.fspath
        chain = [x for x in node.listchain() if x.fspath == path]
        chain = chain[1:]
        names = [x.name for x in chain if x.name != "()"]
        relpath = path.relto(self.topdir)
        if not relpath:
            assert path == self.topdir
            path = ''
        else:
            path = relpath
            if os.sep != "/":
                path = str(path).replace(os.sep, "/")
        names.insert(0, path)
        return "::".join(names)

    def getbyid(self, id):
        """ return one or more nodes matching the id. """
        names = [x for x in id.split("::") if x]
        if names and '/' in names[0]:
            names[:1] = names[0].split("/")
        return self._match([self._topcollector], names)

    def _match(self, matching, names):
        while names:
            name = names.pop(0)
            l = []
            for current in matching:
                for x in current._memocollect():
                    if x.name == name:
                        l.append(x)
                    elif x.name == "()":
                        names.insert(0, name)
                        l.append(x)
                        break
            if not l:
                raise ValueError("no node named %r below %r" %(name, current))
            matching = l
        return matching

    def perform_collect(self):
        nodes = []
        for arg in self.config.args:
            names = self._parsearg(arg)
            try:
                self.genitems([self._topcollector], names, nodes)
            except NoMatch:
                raise self.config.Error("can't collect: %s" % (arg,))
        return nodes

    def genitems(self, matching, names, result):
        if not matching:
            assert not names
            return
        if names:
            name = names[0]
            names = names[1:]
        else:
            name = None
        for node in matching:
            if isinstance(node, pytest.collect.Item):
                if name is None:
                    node.ihook.pytest_log_itemcollect(item=node)
                    result.append(node)
                continue
            assert isinstance(node, pytest.collect.Collector)
            node.ihook.pytest_collectstart(collector=node)
            rep = node.ihook.pytest_make_collect_report(collector=node)
            #print "matching", rep.result, "against name", name
            if rep.passed:
                if not name:
                    self.genitems(rep.result, [], result)
                else:
                    matched = False
                    for x in rep.result:
                        try:
                            if x.name == name or x.fspath.basename == name:
                                self.genitems([x], names, result)
                                matched = True
                            elif x.name == "()": # XXX special Instance() case
                                self.genitems([x], [name] + names, result)
                                matched = True
                        except NoMatch:
                            pass
                    if not matched:
                        node.ihook.pytest_collectreport(report=rep)
                        raise NoMatch(name)
            node.ihook.pytest_collectreport(report=rep)
            x = getattr(self, 'shouldstop', None)
            if x:
                raise Session.Interrupted(x)

class NoMatch(Exception):
    """ raised if genitems cannot locate a matching names. """

def gettopdir(args):
    """ return the top directory for the given paths.
        if the common base dir resides in a python package
        parent directory of the root package is returned.
    """
    fsargs = [py.path.local(decodearg(arg)[0]) for arg in args]
    p = fsargs and fsargs[0] or None
    for x in fsargs[1:]:
        p = p.common(x)
    assert p, "cannot determine common basedir of %s" %(fsargs,)
    pkgdir = p.pypkgpath()
    if pkgdir is None:
        if p.check(file=1):
            p = p.dirpath()
        return p
    else:
        return pkgdir.dirpath()

def decodearg(arg):
    arg = str(arg)
    return arg.split("::")


