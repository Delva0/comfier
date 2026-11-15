"""The same complete H3 preparation and three compute paths using source artifacts.

Only identity (an in-memory computation) is shipped as Python source.
Environment, versions, model selection, verification and PNG I/O use management
and files APIs. Arguments are the same as minimax_h3_remote.py.
"""

import argparse
import asyncio

import minimax_h3_remote as h3

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    h3.add_arguments(parser)
    asyncio.run(h3.run(parser.parse_args(), source=True))
