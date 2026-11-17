"""Real CPU execution verifies roots independently of cached SDK metadata."""

import os
from pathlib import Path

import pytest

import comfier
from comfier import Client, Expired, InProcessTransport
from comfier.daemon import NativeRuntime

COMFY = os.environ.get('COMFIER_COMFY_ROOT', '/home/delva/projects/ComfyUI')
pytestmark = pytest.mark.skipif(not Path(COMFY, 'execution.py').is_file(), reason='real ComfyUI required')


class Value:
    RETURN_TYPES = ('*',)
    FUNCTION = 'run'

    @classmethod
    def INPUT_TYPES(cls):
        return {'required': {'value': ('*',)}}

    def run(self, value):
        return (value,)


class Effect(Value):
    OUTPUT_NODE = True
    seen = []

    def run(self, value):
        self.seen.append(value)
        return (value,)


async def test_native_output_roots_exports_explicit_selection_composition_and_observation():
    runtime = await NativeRuntime(COMFY, cpu=True, custom_nodes=False).start()
    runtime.adapter.nodes.NODE_CLASS_MAPPINGS.update(IndependentValue=Value, IndependentEffect=Effect)
    Effect.seen.clear()
    try:
        async with Client(transport=InProcessTransport(runtime)) as remote:
            raw = {
                'native': {'class_type': 'IndependentEffect', 'inputs': {'value': 'native-root'}},
                'export': {'class_type': 'IndependentValue', 'inputs': {'value': 'exported'}},
            }
            # Simulate cold/old metadata: daemon still knows native OUTPUT_NODE.
            remote._nodes.pop('IndependentEffect')
            imported = comfier.graph(raw, outputs={'value': ['export', 0]})
            first = comfier.submit(imported, return_call=True)
            assert await (await first.output('value')).fetch() == 'exported'
            assert Effect.seen == ['native-root']
            # An explicit target selects roots; it does not pull native effects back in.
            selected = comfier.graph({
                'native': {'class_type': 'IndependentEffect', 'inputs': {'value': 'unselected'}},
                'chosen': {'class_type': 'IndependentValue', 'inputs': {'value': 'chosen'}},
            }, outputs={'value': ['chosen', 0]}, targets=['chosen'])
            with comfier.graph() as composed:
                call = comfier.submit(selected, return_call=True)
                composed.output('value', call.output('value'))
            assert await (await comfier.submit(composed, return_call=True).output('value')).fetch() == 'chosen'
            assert Effect.seen == ['native-root']
            # Merely observing an unexecuted reference does not add a new root.
            with comfier.graph() as capture:
                unused = comfier.node('IndependentValue', value='unobserved')
                chosen = comfier.node('IndependentValue', value='visible')
                capture.target(chosen)
            observed = comfier.submit(capture, return_call=True)
            await observed
            with pytest.raises(comfier.Skipped):
                await observed[unused]
            assert Effect.seen == ['native-root']
            # Exported results are borrowed native cache entries, not permanent pins.
            ref = await first.output('value')
            assert ref.ownership == 'borrowed'
            runtime.adapter.executor.caches.outputs.cache.clear()
            with pytest.raises(Expired):
                await ref.fetch()
    finally:
        await runtime.close()
