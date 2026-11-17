"""Start the independent management server in the selected interpreter."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from aiohttp import web

from comfier.daemon.management_http import create_management_app
from comfier.daemon.supervisor import RuntimeConfig, RuntimeSupervisor


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run a management daemon and supervise one native ComfyUI runtime",
        epilog="Connection never installs or starts ComfyUI. CLI retains automatic runtime startup for existing launch scripts; --no-start runs management only.")
    parser.add_argument("--config", help="Runtime configuration JSON; explicit flags override it")
    parser.add_argument("--print-config", action="store_true")
    parser.add_argument("--comfy-root")
    parser.add_argument("--runtime-python")
    parser.add_argument("--workspace")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8190)
    parser.add_argument("--token", default=os.environ.get("COMFIER_TOKEN", os.environ.get("COMANI_TOKEN")))
    parser.add_argument("--compute-transport", choices=("auto", "http", "zmq"),
                        help="Diagnostic override for the native worker channel (default: auto)")
    parser.add_argument("--zmq-bind", action="append", default=[], metavar="ENDPOINT",
                        help="Additional deployed compute endpoint; remote TCP requires CURVE and COMFIER_TOKEN")
    parser.add_argument("--cpu", action="store_true", default=None)
    parser.add_argument("--no-custom-nodes", action="store_true", default=None)
    parser.add_argument("--no-start", action="store_true", help="Start only management; runtime starts explicitly through the API")
    parser.add_argument("--offline-grace", type=float)
    parser.add_argument("--max-sessions", type=int)
    parser.add_argument("--max-objects", type=int)
    parser.add_argument("--max-call-records", type=int)
    parser.add_argument("--max-file-bytes", type=int)
    parser.add_argument("--transfer-idle-timeout", type=float)
    parser.add_argument("--transfer-limit", type=int)
    parser.add_argument("--transfer-owner-limit", type=int)
    parser.add_argument("--startup-timeout", type=float)
    parser.add_argument("comfy_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    try:
        local = args.host == "localhost" or ipaddress.ip_address(args.host).is_loopback
    except ValueError:
        local = False
    if not local and not args.token:
        parser.error("A token is required to bind a non-loopback address")
    try:
        values = json.loads(Path(args.config).read_text()) if args.config else {}
        if not isinstance(values, dict):
            raise ValueError("The runtime config must be a JSON object")
        for name in ("comfy_root", "runtime_python", "workspace", "cpu", "offline_grace",
                     "max_sessions", "max_call_records", "max_objects", "max_file_bytes", "startup_timeout",
                     "transfer_idle_timeout", "transfer_limit", "transfer_owner_limit"):
            if getattr(args, name) is not None:
                values[name] = getattr(args, name)
        if args.compute_transport is not None:
            values["compute_transport"] = args.compute_transport
        if args.no_custom_nodes is not None:
            values["custom_nodes"] = not args.no_custom_nodes
        if args.comfy_args:
            values["comfy_args"] = args.comfy_args[1:] if args.comfy_args[0] == "--" else args.comfy_args
        if not values.get("comfy_root"):
            parser.error("--comfy-root or config.comfy_root is required")
        values.setdefault("runtime_python", sys.executable)
        values.setdefault("workspace", str(Path.cwd()))
        config = RuntimeConfig(**values)
    except (OSError, ValueError, TypeError) as error:
        parser.error(str(error))
    if args.print_config:
        print(json.dumps(asdict(config), indent=2))
        return
    if args.zmq_bind or config.compute_transport == "zmq":
        from comfier._zmq_protocol import dependency

        try:
            dependency()
        except ImportError as error:
            parser.error(str(error))
    supervisor = RuntimeSupervisor(config, config_path=args.config)
    try:
        app = create_management_app(supervisor, token=args.token, auto_start=not args.no_start,
                                    prepare_download_tools=True, compute_endpoints=args.zmq_bind,
                                    curve_public_key=os.environ.get("COMFIER_ZMQ_CURVE_PUBLIC_KEY", os.environ.get("COMANI_ZMQ_CURVE_PUBLIC_KEY")),
                                    curve_secret_key=os.environ.get("COMFIER_ZMQ_CURVE_SECRET_KEY", os.environ.get("COMANI_ZMQ_CURVE_SECRET_KEY")))
    except ValueError as error:
        parser.error(str(error))
    web.run_app(app,
                host=args.host, port=args.port, handler_cancellation=True)
