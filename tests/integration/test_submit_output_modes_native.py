"""Real native tests distinguish export readiness from workflow completion."""

import asyncio
import gc
import threading
import uuid

import pytest
from PIL import Image

import comfier
from tests.unit._helpers import eventually


class SubmitHead:
    RETURN_TYPES = ('*',)
    FUNCTION = 'run'

    @classmethod
    def INPUT_TYPES(cls):
        return {'required': {'value': ('*',)}}

    def run(self, value):
        return (value,)


class SubmitTail(SubmitHead):
    OUTPUT_NODE = True
    RETURN_TYPES = ()
    started, gate = threading.Event(), threading.Event()

    def run(self, value):
        self.started.set()
        if not self.gate.wait(5):
            raise RuntimeError('tail test gate timed out')
        raise RuntimeError('native workflow tail failed')


class SubmitEffect(SubmitHead):
    OUTPUT_NODE = True
    RETURN_TYPES = ()
    seen = []

    def run(self, value):
        self.seen.append(value)
        return ()


@pytest.mark.parametrize('return_call', [False, True])
async def test_native_export_waits_for_tail_and_propagates_failure(native_http, return_call):
    runtime, url = native_http
    await runtime.start()
    runtime.adapter.nodes.NODE_CLASS_MAPPINGS.update(SubmitHead=SubmitHead, SubmitTail=SubmitTail)
    SubmitTail.started.clear()
    SubmitTail.gate.clear()
    try:
        async with comfier.Client(url):
            with comfier.graph() as graph:
                head = comfier.node('SubmitHead', value=37)
                comfier.node('SubmitTail', value=head)
                graph.output('value', head)
            result = comfier.submit(graph, return_call=return_call)
            output = result.output('value') if return_call else result
            waiter = asyncio.ensure_future(output)
            assert await asyncio.to_thread(SubmitTail.started.wait, 3)
            if return_call:
                assert await (await result[head]).fetch() == 37
            assert not waiter.done()
            SubmitTail.gate.set()
            with pytest.raises(comfier.RemoteError, match='native workflow tail failed'):
                await waiter
            if return_call:
                with pytest.raises(comfier.RemoteError, match='native workflow tail failed'):
                    await result
    finally:
        SubmitTail.gate.set()


async def test_native_zero_exports_execute_raw_and_capture_without_public_handle(native_http):
    runtime, url = native_http
    await runtime.start()
    runtime.adapter.nodes.NODE_CLASS_MAPPINGS['SubmitEffect'] = SubmitEffect
    SubmitEffect.seen.clear()
    async with comfier.Client(url):
        assert comfier.submit({'effect': {'class_type': 'SubmitEffect', 'inputs': {'value': 'raw'}}}) is None
        with comfier.graph() as graph:
            assert comfier.node('SubmitEffect', value='capture') is None
        assert comfier.submit(graph) is None
        gc.collect()
        await eventually(lambda: len(SubmitEffect.seen) == 2)
        assert set(SubmitEffect.seen) == {'raw', 'capture'}
        handle = comfier.submit(graph, return_call=True)
        assert await handle == ()
        assert await handle.files() == ()
        with pytest.raises(ValueError, match='found 0'):
            await handle.file()


async def test_explicit_workflow_call_retains_native_file_and_whole_run_wait(native_http, tmp_path):
    _, url = native_http
    async with comfier.Client(url):
        with comfier.graph() as graph:
            image = comfier.node('EmptyImage', width=8, height=8, batch_size=1, color=0x4080C0)
            saved = comfier.node('SaveImage', images=image,
                                filename_prefix='independent-submit/' + uuid.uuid4().hex,
                                return_call=True)
        run = comfier.submit(graph, return_call=True)
        assert isinstance(run, comfier.Call) and await run == ()
        output = await run.file(node_id=saved.id)
        try:
            downloaded = await comfier.files.download(output, tmp_path / 'workflow.png')
            assert Image.open(downloaded.local_path).getpixel((0, 0)) == (64, 128, 192)
        finally:
            await comfier.files.remove(output)
