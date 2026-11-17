"""Failed async-with entry must reclaim every HTTP session and sender task."""
import argparse
import asyncio
import contextlib
import json
import socket
from pathlib import Path

import aiohttp

from comfier import Client


async def main(args):
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))  # reserved but not listening: deterministic refusal
        port = reserved.getsockname()[1]
        client = Client(f"http://127.0.0.1:{port}")
        failure = None
        try:
            try:
                async with client:
                    raise AssertionError("unexpected connection")
            except aiohttp.ClientConnectorError as error:
                failure = type(error).__name__
            await asyncio.sleep(.01)
            sessions = {name: getattr(client.transport, name, None) is None or getattr(client.transport, name).closed
                        for name in ("_session", "_wait_session", "_data_session")}
            sender_stopped = client._sender is None or client._sender.done()
            result = {"expected_connection_error": failure,
                      "http_session_closed": sessions, "client_closed": client.closed,
                      "sender_stopped": sender_stopped,
                      "passed": bool(failure) and all(sessions.values()) and sender_stopped}
            args.output.write_text(json.dumps(result, indent=2))
            print(json.dumps(result), flush=True)
        finally:
            with contextlib.suppress(Exception):
                await client.close(wait=False)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("tests/stress/failed_open_results.json"))
    raise SystemExit(asyncio.run(main(parser.parse_args())))
