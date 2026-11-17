"""Real CPU user stories and explicit source transfer across Python versions."""

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from PIL import Image

import comfier
from comfier.daemon.management_http import create_management_app
from comfier.daemon.supervisor import RuntimeConfig, RuntimeSupervisor
from tests.unit.test_binary_fetch import serve

PROJECT = Path(__file__).resolve().parents[2]
COMFY = Path(os.environ.get('COMFIER_COMFY_ROOT', PROJECT.parent / 'ComfyUI'))
PYTHON312 = PROJECT / '.local/template-acceptance/python312/bin/python'
SDK = PROJECT / '.venv-sdk/bin/python'
pytestmark = pytest.mark.skipif(not (COMFY / 'execution.py').is_file(), reason='real ComfyUI required')


@asynccontextmanager
async def managed_cpu(tmp_path, python=sys.executable):
    for name in ('input', 'output', 'temp'):
        (tmp_path / name).mkdir(parents=True)
    supervisor = RuntimeSupervisor(RuntimeConfig(
        str(COMFY), runtime_python=str(python), workspace=str(tmp_path), cpu=True,
        custom_nodes=False, comfy_args=(
            '--disable-partner-nodes', '--input-directory', str(tmp_path / 'input'),
            '--output-directory', str(tmp_path / 'output'), '--temp-directory', str(tmp_path / 'temp'))))
    async with serve(create_management_app(supervisor)) as url:
        yield supervisor, url


@pytest.mark.skipif(not SDK.is_file(), reason='Torch-free SDK environment required')
async def test_basic_example_real_cpu_upload_inline_python_native_file_and_download(tmp_path):
    source, target = tmp_path / 'source.png', tmp_path / 'result.png'
    Image.new('RGB', (12, 8), (64, 128, 192)).save(source)
    async with managed_cpu(tmp_path / 'native') as (supervisor, url):
        environment = dict(os.environ)
        environment.pop('PYTHONPATH', None)
        environment.pop('COMFIER_TOKEN', None)
        child = await asyncio.create_subprocess_exec(
            str(SDK), str(PROJECT / 'examples/basic.py'), '--url', url,
            '--input', str(source), '--output', str(target), cwd=PROJECT, env=environment,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(child.communicate(), 90)
        assert child.returncode == 0, (stdout + stderr).decode()
        result = Image.open(target)
        assert result.size == (12, 8)
        assert result.getpixel((0, 0)) == (223, 191, 159)
        assert str(target) in stdout.decode()
        assert not list((tmp_path / 'native/input').rglob('*.png'))
        assert list((tmp_path / 'native/output').rglob('*.png'))
        assert supervisor.state == 'running'


@pytest.mark.skipif(not PYTHON312.is_file(), reason='Existing Python 3.12 native environment required')
async def test_inline_and_path_source_real_python311_client_python312_native(tmp_path):
    source = tmp_path / 'ops.py'
    source.write_text('def main(value):\n    return {"value": value * 3, "items": (1, 2)}\n')
    async with managed_cpu(tmp_path / 'native', PYTHON312) as (_, url):
        async with comfier.Client(url):
            await comfier.comfyui.start()
            value = comfier.python(code=source.read_text(), entry='main', inputs={'value': 7})
            assert await (await value).fetch() == {'value': 21, 'items': (1, 2)}
            call = comfier.python(source=source, entry='main', inputs={'value': 9}, return_call=True)
            ref, = await call
            assert await ref.fetch() == {'value': 27, 'items': (1, 2)}
            with comfier.graph() as graph:
                result = comfier.python(code='def main(value):\n    return value + 1\n',
                                       entry='main', inputs={'value': 41})
                graph.output('value', result)
            run = comfier.submit(graph, return_call=True)
            assert await (await run.output('value')).fetch() == 42
            with pytest.raises(comfier.CodeCompatibilityError, match='exactly matching'):
                await comfier.python(lambda: 1)
            with pytest.raises(comfier.RemoteError, match='captured_global'):
                await comfier.python(code='def main():\n    return captured_global\n', entry='main')


async def test_module_routing_real_http_twenty_four_client_tasks(native_http):
    _, url = native_http

    async def workflow(index):
        async with comfier.Client(url):
            value = comfier.python(lambda item: item, inputs={'item': index * 100})
            for _ in range(4):
                value = comfier.python(lambda item: item + 1, inputs={'item': value})
                await asyncio.sleep(0)
            with comfier.graph() as graph:
                port = graph.input('source')
                output = comfier.python(code='def main(item):\n    return item + 1\n',
                                       entry='main', inputs={'item': port})
                graph.output('value', output)
            run = comfier.submit(graph, inputs={'source': value}, return_call=True)
            # Adopt the borrowed graph value before another native run evicts it.
            retained = comfier.python(lambda item: item, inputs={'item': run.output('value')})
            return await (await retained).fetch()

    assert await asyncio.gather(*(workflow(i) for i in range(24))) == [i * 100 + 5 for i in range(24)]


@pytest.mark.skipif(not SDK.is_file(), reason='Torch-free SDK environment required')
async def test_graph_to_eager_example_real_cpu_default_submit_export(tmp_path):
    target = tmp_path / 'graph-to-eager.png'
    async with managed_cpu(tmp_path / 'native') as (_, url):
        environment = dict(os.environ)
        environment.pop('PYTHONPATH', None)
        environment.pop('COMFIER_TOKEN', None)
        child = await asyncio.create_subprocess_exec(
            str(SDK), str(PROJECT / 'examples/graph_to_eager.py'), '--url', url,
            '--output', str(target), cwd=PROJECT, env=environment,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(child.communicate(), 90)
        assert child.returncode == 0, (stdout + stderr).decode()
        result = Image.open(target)
        assert result.size == (64, 64)
        assert all(abs(actual - expected) <= 1
                   for actual, expected in zip(result.getpixel((0, 0)), (18, 52, 86)))
        assert str(target) in stdout.decode()
