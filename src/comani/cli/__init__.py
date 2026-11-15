"""The public command-line application, above the daemon implementation."""

import argparse
import sys


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["daemon"]:
        from .daemon import main as launch

        return launch(argv[1:])
    parser = argparse.ArgumentParser(description="Comani SDK and remote management")
    parser.add_argument("command", choices=["daemon"],
                        help="Run management using the current configured Python environment")
    parser.parse_args(argv)
