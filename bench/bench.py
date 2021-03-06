import sys

if __name__ == '__main__':
    import cProfile
    import py
    import pstats
    script = sys.argv[1] if len(sys.argv) > 1 else "empty.py"
    stats = cProfile.run('py.test.cmdline.main([%r])' % script, 'prof')
    p = pstats.Stats("prof")
    p.strip_dirs()
    p.sort_stats('cumulative')
    print(p.print_stats(250))
