# V1 delivery record

This is the historical pre-migration record. Paths containing `next/` describe
the original measurement location; the project now lives at the repository root.
Use [the development guide](design/development.md) for current commands.
The script-based deployment below belongs to the
[758738e baseline](https://github.com/Delva0/comani/tree/758738ee07e1d13223fde9013effd4fa6286f942),
not the current CLI. Archived sources:
[bootstrap_runtime.py](https://github.com/Delva0/comani/blob/758738ee07e1d13223fde9013effd4fa6286f942/scripts/bootstrap_runtime.py),
[check_environment.py](https://github.com/Delva0/comani/blob/758738ee07e1d13223fde9013effd4fa6286f942/scripts/check_environment.py),
[dev_server.py](https://github.com/Delva0/comani/blob/758738ee07e1d13223fde9013effd4fa6286f942/scripts/dev_server.py),
[example.py](https://github.com/Delva0/comani/blob/758738ee07e1d13223fde9013effd4fa6286f942/scripts/example.py).
The measurements and raw evidence remain the original results; later startup
changes require their own acceptance.

## Result

The approved v1 scope is implemented and verified on ComfyUI
`eee59185ff4f43ae2d9ad122b245619d27d9daad`, Python 3.11.11 and cloudpickle 3.1.2.
The final non-GPU regression is **70 passed, 2 GPU tests deselected, 22.05 s**.
The two separate real GPU/model checks are **2 passed, 64.98 s**. Lint passes.
These are actual executions, not estimates or mocked replacements for native gates.

## Requirement evidence matrix

Paths below are relative to `next/tests/`. The native and HTTP tests execute the
real adjacent ComfyUI checkout. Unit peers are used for deterministic client races.

| Requirement | Concrete evidence | Result |
| --- | --- | --- |
| Immediate submission; Call/OutputFuture/RemoteRef separation; await versus fetch | `unit/test_sdk.py`: unawaited calls, ready ref, unknown layout; `integration/test_http.py`: unawaited real call | Verified |
| Dependencies, unrelated admission, failures and cancellation | Unit pending/failure/cancel tests; `native/test_gate.py::test_native_non_output_external_object_and_queue`; HTTP pending dependency | Verified |
| Synchronous native work does not block HTTP submit/heartbeat | `integration/test_http.py::test_sync_native_work_does_not_block_submit_or_heartbeat`: ordinary `time.sleep(.6)`, ACK/heartbeat before completion | Verified |
| Eager owned objects, aliases, automatic GC, in-flight inputs | Unit ownership/retained-slot/cancel tests; native duplicate dependency; HTTP GC with repeated input refs | Verified |
| Metadata cleanup rather than hidden ownership roots | HTTP `test_completed_call_records_and_objects_are_collected`: 20 calls, session call/object tables return to zero | Verified |
| Disconnect/session lease; long active wait without heartbeat | Native abandoned session; HTTP `test_active_http_wait_renews_session_then_disconnect_expires`: TTL .15 s, synchronous .5 s, then abrupt transport close | Verified |
| Graph capture, symbolic call unwrap, run binding and exception restoration | Unit capture/restoration; HTTP `test_workflow_intermediate_ready_before_whole_workflow` | Verified |
| Composition, reuse, input signature and eager object binding | Unit composition; HTTP eager-object workflow; HTTP nested file/package graph returns 21 | Verified |
| Native API prompt JSON and explicit execution targets | HTTP `test_workflow_reuses_eager_object_and_native_prompt`; model raw workflow; native target validation | Verified |
| Callable, source file and source directory; graph Python | Native one-slot values; HTTP eager artifacts and nested graph file/package tests | Verified |
| Normal directory imports, module state and explicit conflicts | HTTP `test_package_normal_imports_globals_and_conflicts`: absolute+relative imports, async, short/full entries, stable mtime/chmod hash, cross-session counter, typed same-name/installed-package conflicts | Verified |
| Lists/tuples/dicts remain one Python result; native mapping remains distinct | Native parametrized Python slots and native list mapping; unit tree/layout; HTTP nested argument values | Verified |
| Native V1/V3, lazy, expansion, hidden inputs and rawLink/loop structure | `native/test_gate.py`: lazy/expand, hidden/rawLink, official V3 dynamic-combo carried-value loop with repeated template | Verified |
| Backend UI/history, SaveImage and hidden PNG prompt | HTTP `test_native_prompt_history_ui_events_and_hidden_prompt`: `/prompt`, `/ws`, `/history`, temporary PNG and prompt metadata | Verified |
| Native progress and preview events | Native ProgressBar/PIL preview test and hook restoration; JSON-safe bytes/image event codec | Verified |
| Borrowed workflow cache, Expired, no resurrection and accepted input protection | HTTP explicit cache-clear Expired; native same-key recomputation and temporary input-pin tests | Verified |
| Real GPU node -> Python -> node | `integration/test_gpu.py::test_gpu_node_python_node_matches_native_function`: CUDA result equals native function exactly | Verified |
| Existing checkpoint eager/native workflow comparison | `integration/test_gpu.py::test_existing_checkpoint_eager_matches_native_workflow`: 64x64, seed 42, 1 Euler step; max absolute float error **0.0** | Verified |
| SDK without torch/ComfyUI; wheel install and real wheel client | `integration/test_sdk_environment.py`: SDK-only child, installed wheel from `/tmp`, CLI help, real HTTP eager/graph result 21 | Verified |
| Loopback/authenticated transport and local deployment | HTTP bearer test; native Host/Origin/token tests; recorded start/status/stop and actual `scripts/example.py` | Verified |

No item in the approved v1 scope is left pending. This matrix is not a statement
that every future ComfyUI version, model, device backend or third-party plugin has
been exercised. Frontend-only nodes and canvas conversion are outside scope.

## Reproducible commands and results

Run from `/home/delva/projects/comani/next`:

```sh
python3 scripts/bootstrap_runtime.py \
  --python /home/delva/projects/comani/.venv/bin/python \
  --comfy-root /home/delva/projects/ComfyUI
.venv/bin/python -B scripts/check_environment.py \
  --comfy-root /home/delva/projects/ComfyUI --cuda
.venv/bin/python -B -m pytest -q -m 'not gpu' -p no:cacheprovider
.venv/bin/ruff check src tests scripts
```

Bootstrap was actually run after initial dependency preparation: all **168**
installed distributions were compatible. The native gate resolves `comani` from
`next/src/comani`, torch from `next/.venv`, and `execution.py` from the pinned
ComfyUI checkout; a real CUDA 3x3 matrix operation matches its explicit reference.
The final CPU/native/unit/HTTP/wheel regression yielded 70 passed in 22.05 s.
Warnings are third-party deprecation/AppKey notices, not failed assertions.

```sh
.venv/bin/python -B scripts/dev_server.py start \
  --comfy-root /home/delva/projects/ComfyUI --port 8190
COMANI_TEST_URL=http://127.0.0.1:8190 \
COMANI_TEST_CHECKPOINT=astralsea_v12.safetensors \
  .venv/bin/python -B -m pytest -q tests/integration/test_gpu.py -p no:cacheprovider
.venv/bin/python -B scripts/dev_server.py stop
```

Both GPU tests passed in 64.98 s using existing weights, with no model download
or copy. The final raw call was `3ef837564f2e4f9fba07e2da139e2941`; its explicit
consumer was `cdd0a56c6af44110a437e8fa987c7501`. Local diagnostic stage records are
in `.local/model-validation.jsonl`. The recorded daemon was stopped only after
its requests were terminal, and port 8190 was confirmed released.

```sh
~/.local/bin/uv build --wheel --out-dir .local/dist
~/.local/bin/uv venv --python .venv/bin/python .venv-wheel
~/.local/bin/uv pip install --python .venv-wheel/bin/python --reinstall \
  .local/dist/comani-0.2.0-py3-none-any.whl
```

The installed wheel was verified from `/tmp`, with no torch installed in that
client environment; it executed both eager Python and a graph over real HTTP.
`python -m comani._runtime --help` also ran from the installed wheel. The wheel
hash and actual source counts are in `test-results.json` and `measurements.json`.

The documented bootstrap, launcher and `scripts/example.py` were run: eager,
captured workflow and native JSON all returned shape `[1,64,64,3]`; the captured
intermediate reported `borrowed`. See the README for API snippets and commands.

## Resource and ownership observations

The model comparison initially retained eager MODEL/CLIP/VAE refs while loading
the reference graph. On this 8 GiB WSL machine, native post-execution RAM cleanup
and swapping delayed completion; one observation hit 240 s. A later completed
workflow correctly returned `Expired` when its borrowed image had been evicted.
Neither condition was hidden by restarting the worker or changing cache policy.

The final comparison returns only the host image from the eager function scope,
then verifies actual release/forget ACK completion before loading the reference.
It explicitly submits an eager identity consumer of the workflow output. The
accepted consumer's temporary input protection is the existing dependency
mechanism; the original workflow ref remains borrowed and may still expire.
The returned eager image is owned and safe to fetch for the exact float comparison.
Timeout and native memory policy were unchanged. On the preserved process,
VmSwap was 6,967,552 kB before the run, 7,444,448 kB after eager-scope release and
7,711,232 kB at success; final RSS was 3,471,596 kB. These are observations, not
claims of a performance or memory guarantee. The process was stopped afterward.

## Environment and compatibility boundaries

The independent runtime environment was seeded by copying third-party packages
as independent files, then resolving current requirements. The old ComfyUI venv
was not modified: its torch metadata remains 2.9.1, av 16.0.1, frontend 1.28.8,
numpy 2.3.5 and xformers 0.0.33.post1. New environment versions are recorded in
`environment.json`; xformers was removed only from the new environment to resolve
its incompatible torch pin. The GPU is one RTX 4060 Laptop (8 GiB), CUDA build
12.8. ComfyUI emits a cu130+ warning for newer optimized operations; the tested
native/GPU/checkpoint paths passed, not every optimized kernel or model.

The API prompt graph format is supported, not frontend canvas JSON. Driver
`/prompt` acknowledgements and `/history` completion/UI output were tested.
The driver does not implement all ComfyUI HTTP endpoints or frontend behavior:
queue/event envelopes differ, binary previews are JSON-safe base64 records,
validation errors may be asynchronous execution failures, and `/view` is absent.

Directory code uses the selected normal-process Python package semantics:
absolute/relative imports work, identical hashes reuse module globals across
sessions, and same-name different-hash or installed-package conflicts raise
`PackageConflict`. Session close does not unload modules. Restart selects a new
version of a package. Source transport does not synchronize dependencies.

## Preserved checkout and rollback

Implementation lives entirely under `next/`; the old root package/config/CLI
were not replaced. Initial and final legacy status excluding `next/` match:
89 deleted tracked entries, 7 modified entries, 224 untracked entries at
`348d2aa8f7e7aeacf259cad99851bb8ffebd5fe1`. No Git commit/push/reset was performed.
ComfyUI remains clean at `eee59185ff4f43ae2d9ad122b245619d27d9daad`.
Models were referenced in place, not copied or altered. No credentials file was
read. Environment and artifact directories are excluded from project code counts.

Rollback requires stopping only the recorded daemon with
`scripts/dev_server.py stop`, then ceasing use of `next/.venv` and its entrypoint.
No old environment or ComfyUI source restoration is needed. The launcher checks
PID start identity and refuses occupied ports; it does not kill unrelated services.
