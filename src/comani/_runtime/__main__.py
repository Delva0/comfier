import argparse
import importlib.util
import ipaddress
import os
from pathlib import Path

from aiohttp import web

from .http import create_app
from .service import NativeRuntime


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run one native ComfyUI runtime in this configured Python environment",
        epilog="Arguments after -- configure native runtime initialization. Native HTTP/UI/asset-server options are rejected. Comani does not install ComfyUI or its dependencies.",
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
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    if not (Path(args.comfy_root).expanduser() / "execution.py").is_file():
        parser.error("--comfy-root must point to an existing ComfyUI source directory in this configured environment")
    loopback = args.host == "localhost"
    try:
        loopback = loopback or ipaddress.ip_address(args.host).is_loopback
    except ValueError:
        pass
    if not loopback and not args.token:
        parser.error("A token is required to bind a non-loopback address")
    try:
        missing = [name for name in ("torch", "yaml") if importlib.util.find_spec(name) is None]
        if missing:
            parser.error("The current Python environment lacks native ComfyUI dependencies (" + ", ".join(missing) + "); use the interpreter prepared by your template or deployment")
        runtime = NativeRuntime(
            Path(args.comfy_root).expanduser(), cpu=args.cpu, custom_nodes=not args.no_custom_nodes, comfy_args=comfy_args,
        )
    except ModuleNotFoundError as error:
        parser.error(f"The current environment is missing a native dependency: {error.name}")
    web.run_app(create_app(runtime, token=args.token), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
