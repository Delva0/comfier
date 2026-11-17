"""Private native-worker bootstrap; this is not a command-line application."""

from pathlib import Path


def run(config, *, socket_fd=None, port=None, token=None, compute_endpoint=None):
    """Run the one child selected by RuntimeSupervisor from an explicit config."""
    import importlib.util
    import socket

    from aiohttp import web

    from .http import create_runtime_app
    from .service import NativeRuntime, RecoveryPolicy

    root = Path(config["comfy_root"]).expanduser()
    if not (root / "execution.py").is_file():
        raise ValueError("comfy_root must point to the configured ComfyUI source directory")
    missing = [name for name in ("torch", "yaml") if importlib.util.find_spec(name) is None]
    if missing:
        raise RuntimeError("The selected runtime environment lacks native dependencies: " + ", ".join(missing))
    runtime = NativeRuntime(
        root, cpu=config.get("cpu", False), custom_nodes=config.get("custom_nodes", True),
        comfy_args=config.get("comfy_args", ()), max_file_bytes=config.get("max_file_bytes"),
        recovery_policy=RecoveryPolicy(offline_grace=config.get("offline_grace", 3600),
                                       max_sessions=config.get("max_sessions", 256),
                                       max_objects=config.get("max_objects", 100000),
                                       max_call_records=config.get("max_call_records", 1000000)))
    options = ({"sock": socket.socket(fileno=socket_fd)} if socket_fd is not None
               else {"host": "127.0.0.1", "port": port})
    async def application():
        app = await create_runtime_app(runtime, token=token)
        if compute_endpoint is not None:
            from .zmq import ZMQServer

            server = ZMQServer(runtime, endpoints=[compute_endpoint], token=token)

            async def compute_channel(app):
                try:
                    await server.start()
                except Exception:
                    await server.close()
                    if config.get("compute_transport") != "auto":
                        raise
                    print("Private IPC unavailable; retaining the native HTTP channel", flush=True)
                try:
                    yield
                finally:
                    await server.close()

            app.cleanup_ctx.append(compute_channel)
        return app

    web.run_app(application(), **options)
