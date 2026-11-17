"""Metadata projection never traverses native tensor/object representations."""

import math

from comfier.daemon.service import NativeRuntime


def test_call_metadata_projects_only_safe_native_ui_without_prompt_or_repr():
    class Opaque:
        def __repr__(self):
            raise AssertionError("Native objects must not be represented in metadata")

    cycle = []
    cycle.append(cycle)
    metadata = NativeRuntime._metadata({
        "prompt_id": "call-identity", "executed": ["save"],
        "history": {"outputs": {"save": {"images": [{"filename": "result.png", "type": "output"}],
            "unsafe": Opaque(), "nonfinite": math.inf, "cycle": cycle}},
            "meta": {"secret": "excluded"}},
        "prompt": {"input": "excluded"}, "extra_data": {"token": "excluded"}})
    assert metadata == {"prompt_id": "call-identity", "executed": ["save"], "ui": {
        "save": {"images": [{"filename": "result.png", "type": "output"}], "cycle": []}}}
