"""Run a daemon in the interpreter and native environment chosen by its owner."""

import argparse
import sys


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["daemon"]:
        from ._runtime.__main__ import main as daemon

        return daemon(argv[1:])
    parser = argparse.ArgumentParser(description="Comani SDK and native runtime driver")
    parser.add_argument("command", choices=["daemon"], help="Run using the current, configured Python environment")
    parser.parse_args(argv)


if __name__ == "__main__":
    main()
