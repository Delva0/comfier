"""Cancel real HTTP client entry while its handshake response is gated."""
import argparse
import asyncio
import contextlib
import json
from pathlib import Path

from aiohttp import web

from comani import Client, SessionClosed


async def main(args):
    entered, release = asyncio.Event(), asyncio.Event()

    async def gate(request):
        await request.json()
        entered.set()
        await release.wait()
        return web.json_response({"result": {}})

    app = web.Application()
    app.router.add_post("/rpc", gate)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    client = Client(f"http://127.0.0.1:{port}")
    try:
        call = client.node("NeverSubmitted", return_call=True, value=1)
        waiter = asyncio.create_task(client.start())
        await asyncio.wait_for(entered.wait(), 2)
        waiter.cancel()
        outcomes = await asyncio.gather(waiter, return_exceptions=True)
        assert isinstance(outcomes[0], asyncio.CancelledError)
        output = await asyncio.gather(call._ready(), return_exceptions=True)
        assert isinstance(output[0], SessionClosed)
        sessions = {name: getattr(client.transport, name, None) is None or getattr(client.transport, name).closed
                    for name in ("_session", "_wait_session", "_data_session")}
        stopped = client._sender is None or client._sender.done()
        assert all(sessions.values()) and stopped and client.closed
        result = {"passed": True, "handshake_cancelled": True, "pending_call_error": type(output[0]).__name__,
                  "http_sessions_closed": sessions, "sender_stopped": stopped, "client_closed": client.closed}
        args.output.write_text(json.dumps(result, indent=2))
        print(json.dumps(result), flush=True)
    finally:
        release.set()
        with contextlib.suppress(Exception):
            await client.close(wait=False)
        await runner.cleanup()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("tests/stress/startup_cancel_results.json"))
    asyncio.run(main(parser.parse_args()))
