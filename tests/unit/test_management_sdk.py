"""SDK management semantics through real HTTP and real bash subprocesses."""

import asyncio
from contextlib import asynccontextmanager

import aiohttp
import pytest
from aiohttp import web

from comani import (
    Client,
    CodeCompatibilityError,
    CommandError,
    CommandResult,
    DaemonRestarted,
    InProcessTransport,
    ManagementOperation,
    OperationCancelled,
)
from comani.daemon.management_http import create_management_app
from comani.daemon.supervisor import RuntimeConfig, RuntimeSupervisor

from .conftest import FakeRuntime
from .test_binary_fetch import serve


async def test_management_app_passes_explicit_retention_limits_to_service(tmp_path):
    supervisor = RuntimeSupervisor(RuntimeConfig(str(tmp_path / "uninstalled"), workspace=str(tmp_path)))
    app = create_management_app(supervisor, management_limits={
        "max_records": 3, "max_active": 2, "max_running": 1, "window_seconds": 0.2,
        "result_seconds": 0.1, "max_result_size": 1024, "max_result_bytes": 3072,
        "log_bytes": 256, "max_log_bytes": 512, "capture_bytes": 128})
    async with serve(app) as url:
        async with Client(url) as client:
            limits = client.management_handshake["limits"]
            assert limits["max_operations"] == 3 and limits["max_active"] == 2
            assert limits["max_running"] == 1 and limits["result_seconds"] == 0.1
            assert limits["max_result_bytes"] == 3072 and limits["max_log_bytes"] == 512
    with pytest.raises(ValueError, match="service or management_limits"):
        create_management_app(supervisor, service=object(), management_limits={})


@asynccontextmanager
async def managed_server(tmp_path):
    supervisor = RuntimeSupervisor(RuntimeConfig(str(tmp_path / "uninstalled"), workspace=str(tmp_path)))
    app = create_management_app(supervisor)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        yield app, supervisor, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    finally:
        await runner.cleanup()


async def test_management_only_connect_and_lost_admission_do_not_repeat_shell_effect(tmp_path):
    async with managed_server(tmp_path) as (_, supervisor, url):
        async with Client(url) as remote:
            assert supervisor.process is None and remote._recovery is None
            request, attempts = remote.transport.request, []

            async def drop_admission(method, **params):
                value = await request(method, **params)
                if method == "management:management_begin":
                    attempts.append(params)
                    if len(attempts) == 1:
                        raise aiohttp.ClientConnectionError("Lost admission response")
                return value

            remote.transport.request = drop_admission
            result = await remote.bash.run("printf once >> side-effect; printf output", check=True)
            assert result.stdout == "output" and result.exit_code == 0
            assert (tmp_path / "side-effect").read_text() == "once"
            assert len(attempts) == 2 and attempts[0] == attempts[1]
            assert remote._recovery is None


async def test_cancelling_observation_and_closing_client_leave_task_attachable(tmp_path):
    async with managed_server(tmp_path) as (_, _, url):
        remote = await Client(url).start()
        operation = await remote.bash.begin("sleep 0.2; printf completed > survived", check=True)
        assert isinstance(operation, ManagementOperation)
        handle = operation.export_handle()
        waiter = asyncio.create_task(operation.result())
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        await remote.close()
        async with Client(url) as observer:
            attached = await observer.operations.get(handle=handle)
            assert (await attached.result()).exit_code == 0
            assert (tmp_path / "survived").read_text() == "completed"
            entries = [entry async for entry in attached.logs()]
            assert all(a.cursor < b.cursor for a, b in zip(entries, entries[1:]))


async def test_explicit_cancel_and_command_check_are_distinct(tmp_path):
    async with managed_server(tmp_path) as (_, _, url):
        async with Client(url) as remote:
            with pytest.raises(CommandError) as failure:
                await remote.bash.run("printf error >&2; exit 7", check=True)
            assert isinstance(failure.value.result, CommandResult)
            assert failure.value.result.exit_code == 7
            result = await remote.bash.run("exit 7", check=False)
            assert result.exit_code == 7
            unchecked = await remote.bash.begin("exit 7", check=False)
            attached = await remote.operations.get(handle=unchecked.export_handle())
            assert (await attached.result()).exit_code == 7
            operation = await remote.bash.begin("sleep 30", check=True)
            status = await operation.cancel()
            assert status.state == "cancelled"
            with pytest.raises(OperationCancelled):
                await operation.result()


async def test_management_inside_graph_is_real_io_and_does_not_capture(tmp_path):
    async with managed_server(tmp_path) as (_, _, url):
        async with Client(url) as remote:
            with remote.graph() as graph:
                result = await remote.bash.run("printf local-metadata", check=True)
                assert result.stdout == "local-metadata"
                assert not graph.nodes


async def test_changed_daemon_epoch_after_uncertain_admission_never_sends_a_new_key(tmp_path):
    async with managed_server(tmp_path) as (app, _, url):
        async with Client(url) as remote:
            request, attempts = remote.transport.request, []

            async def restart_after_admission(method, **params):
                value = await request(method, **params)
                if method == "management:management_begin":
                    attempts.append(params)
                    app["management"].operations.epoch = "changed-daemon"
                    raise aiohttp.ClientConnectionError("Old admission outcome became unknown")
                return value

            remote.transport.request = restart_after_admission
            with pytest.raises(DaemonRestarted):
                await remote.bash.run("printf once >> uncertain", check=True)
            assert len(attempts) == 1


@pytest.mark.parametrize("runtime_compatible", [True, False])
async def test_callable_abi_uses_runtime_handshake_even_when_daemon_versions_disagree(runtime_compatible):
    class SeparateInterpreters(FakeRuntime):
        async def management_request(self, method, **params):
            assert method == "handshake"
            import sys

            import cloudpickle
            return {"daemon_epoch": "management-epoch", "session_id": "management-session",
                    "python": [0, 0, 0] if runtime_compatible else list(sys.version_info[:3]),
                    "cloudpickle": "incompatible" if runtime_compatible else cloudpickle.__version__,
                    "capabilities": {"management": 1}}

        async def handshake(self, session_id):
            value = await super().handshake(session_id)
            if not runtime_compatible:
                value.update(python=[0, 0, 0], cloudpickle="incompatible")
            return value

    runtime = SeparateInterpreters()
    async with Client(transport=InProcessTransport(runtime)) as remote:
        assert not runtime.submissions
        call = remote.python(lambda: 42, return_call=True)
        if runtime_compatible:
            assert await (await call.output()).fetch() == 42
        else:
            with pytest.raises(CodeCompatibilityError):
                await call
            assert not runtime.submissions
