import argparse
import ipaddress
import os

from aiohttp import web

from .http import create_app
from .service import NativeRuntime


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="A local Comani driver for one native ComfyUI runtime",
        epilog="Arguments after -- configure native ComfyUI runtime initialization; ComfyUI main.py startup actions are not run.",
    )
    parser.add_argument("--comfy-root", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8190)
    parser.add_argument("--token", default=os.environ.get("COMANI_TOKEN"))
    parser.add_argument("--cpu", action="store_true")
    parser.add_argument("--no-custom-nodes", action="store_true")
    parser.add_argument("comfy_args", nargs=argparse.REMAINDER, help="Native ComfyUI arguments after --")
    args = parser.parse_args(argv)
    comfy_args = args.comfy_args[1:] if args.comfy_args[:1] == ["--"] else args.comfy_args
    loopback = args.host == "localhost"
    try:
        loopback = loopback or ipaddress.ip_address(args.host).is_loopback
    except ValueError:
        pass
    if not loopback and not args.token:
        parser.error("A token is required to bind a non-loopback address")
    runtime = NativeRuntime(
        args.comfy_root, cpu=args.cpu, custom_nodes=not args.no_custom_nodes, comfy_args=comfy_args,
    )
    web.run_app(create_app(runtime, token=args.token), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
