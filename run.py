import sys

if __name__ == "__main__":
    if "--self-test" in sys.argv:
        from horizonlens.selftest import main
        main()
    else:
        from horizonlens.desktop import run
        run()
