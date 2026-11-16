# Independent RTX 4090 acceptance

## Official ComfyUI template: public cd6 checkpoint

**The requested H3 INT8 model passed on the official ComfyUI template on 2026-11-15 UTC.**
The successful instance was `60741609`. The eager, captured graph and native API
paths produced real 2048×2048 images. The instance was destroyed at
`05:38:56.922227 UTC`; the official instance list was subsequently empty and the
local watchdog was stopped. The [acceptance summary](../artifacts/remote/60741609-20261115/acceptance-summary.json) and [official cleanup observation](../artifacts/remote/60741609-20261115/resource-cleanup.json) retain that boundary.

![Official-template H3 output](../artifacts/remote/60741609-20261115/eager.png)

### Actual scripts and build identity

The executed client is [minimax_h3_source_remote.py](https://github.com/Delva0/comani/blob/42350b082ba29b6b5c9e82fed40858d971fccb8c/examples/minimax_h3_source_remote.py),
with [minimax_h3_source_ops.py](https://github.com/Delva0/comani/blob/42350b082ba29b6b5c9e82fed40858d971fccb8c/examples/minimax_h3_source_ops.py) as explicit
Python source artifacts. They were run before being published with this report;
[run.json](../artifacts/remote/60741609-20261115/run.json) records both file hashes.
The pipeline retains original native node names, places a source-file identity
between KSampler and VAEDecode, and explicitly retains borrowed graph outputs.
It installs no environment, dependencies or models.

The runtime package came from public commit
[cd6f45c](https://github.com/Delva0/comani/tree/cd6f45c8b7da5ca71a1e80f5be5b7fa49993433e).
All 17 installed production Python files match its frozen
[manifest](../tests/acceptance/results/heartbeat-v4-r2/production-manifest.json).
The actual Git-built wheel SHA256 was
`258dbeaaf952f69c43e485ffdcbcf2849ef66810f08f0ea81bde2ef952bae58a`.
The local pre-publication CPU-tested original was `64a73a6e…abd98`; these are two
separate artifacts. ZIP inspection found only METADATA and RECORD member contents
different: the new UTF-8 long description matches the public cd6 README, all 17
package Python files and WHEEL content are identical. No wheel was rebuilt merely
to force the same byte hash. See [archive identity](../artifacts/remote/60741609-20261115/archive-identity.json) and the [measured ZIP difference](../artifacts/remote/60741609-20261115/wheel-build-difference.json).

The template retained Python **3.12.15** and **Torch 2.10.0+cu130** in its existing
interpreter. Its ComfyUI source was explicitly updated to
`eee59185ff4f43ae2d9ad122b245619d27d9daad` for the required native H3 nodes;
this was not an assertion that the template's initial Comfy revision already
contained them. The initial revision was `2c75dc1b379a59e0880619d029607af5169a62ef`, recorded in [initial-system.json](../artifacts/remote/60741609-20261115/initial-system.json). The template was ID `325864`, image `vastai/comfy:v0.44.0-cuda-13.2-py312`; the actual Torch build remained cu130. Torch was not replaced. The genuine project CLI applied native
`--reserve-vram 4`; the measured reserve was 4.0 GiB and native VRAM mode NORMAL_VRAM.
[runtime-provenance.json](../artifacts/remote/60741609-20261115/runtime-provenance.json)
records the actual installed source hashes, environment and three verified model files.
The model and companion SHA256 values match the exact requested files listed in
the historical section below; no different quantization or checkpoint was substituted.

Run the source example from this checkout with an authenticated daemon and the
verified model files already available. COMANI_URL and COMANI_TOKEN come from the
environment; Tensor fetch also requires the client's Tensor dependencies.

```sh
python examples/minimax_h3_source_remote.py \
  --output artifacts/my-h3-run \
  --package-commit cd6f45c8b7da5ca71a1e80f5be5b7fa49993433e \
  --wheel-sha256 258dbeaaf952f69c43e485ffdcbcf2849ef66810f08f0ea81bde2ef952bae58a \
  --verification-path /workspace/comani-acceptance/model-verification.json
```

The actual operator journey has six phases: select the official template and verify its real limits; use its existing interpreter and conditionally update the existing Comfy source for H3 support; download/hash the three weights in native directories; install the fixed public Comani package; replace the template comfyui/api-wrapper process with the thin daemon while reusing the existing authentication/HTTPS route; run the installed public client and explicit source example. No new runtime venv or Torch installation was made. The command was:

```sh
/venv/main/bin/comani daemon --comfy-root /workspace/ComfyUI \
  --host 127.0.0.1 --port 18188 --no-custom-nodes -- --reserve-vram 4
```

Token configuration was supplied through the environment, not printed in the command.
[Remote installation](../artifacts/remote/60741609-20261115/remote-installation.json)
records the actual fixed Git commit. The local client used a normal target installation
from that public commit, selected with PYTHONPATH in an existing Python 3.11.11
interpreter that supplied Tensor dependencies; it did not import editable/src Comani.
[Client installation](../artifacts/remote/60741609-20261115/client-installation.json)
records all 17 installed hashes.

The verification path is a caller-selectable record produced after hashing the
three model files; it is not a model installer. The acceptance endpoint has been
shut down, so a new deployment must provide its own URL/token and environment.

### Three real output paths and long waiting

| Sequential mode | End-to-end seconds | Max absolute float error versus eager |
|---|---:|---:|
| Eager native → source Python → native | 177.788 | 0 |
| Captured graph → explicit eager retention | 152.406 | 0 |
| Native API JSON → explicit eager retention | 13.478 | 0 |

These are observations from that execution order. The example did not reset models/native cache between modes, so warm/cache effects are possible; these timings are not a fair cold-start benchmark or a claim of parallel native computation. Three complete saved float32 tensors were separately
loaded and compared locally: shape `[1,2048,2048,3]`, finite values, torch.equal true,
raw tensor SHA256 `38efaa6cb2da7f55332b1b9584db0359f1084ba563a4bfbaf00dd247d4e4c5d4`.
All three PNGs are 4,817,799 bytes and have SHA256
`db9f84dd9885542f6d02e8f1224552c263dfb2258c5c64c7aa0e145aa46ab886`.
See [image-comparison.json](../artifacts/remote/60741609-20261115/image-comparison.json)
and the actual [native prompt](../artifacts/remote/60741609-20261115/native_prompt.json).
One unchanged original PNG is published; duplicate PNGs and all full `.pt` tensors
remain local and are not public download links.

The new actual HTTPS long task also passed: **130 seconds of remote work,
133.6386 seconds end to end, one submit, seven polls, one execution**.
[long-wait.json](../artifacts/remote/60741609-20261115/long-wait.json) retains the
record. The callable/source boundary remains explicit: source files allow the
3.11 client/3.12 runtime path without sending incompatible callable bytecode.

### Measured resources and cleanup boundary

| Observation | Actual value |
|---|---:|
| Requested / actual filesystem disk | 60 GiB / 64,424,509,440 bytes |
| Final disk used / free | 45,953,425,408 / 18,471,084,032 bytes |
| Quoted RAM | 128,903 MiB |
| Actual enforced cgroup RAM limit | 259,515,219,968 bytes ≈ 241.69 GiB |
| Highest sampled process RSS | 42,607,919,104 bytes |
| Kernel process RSS high-water | 42,742,071,296 bytes |
| Highest sampled cgroup current memory | 48,291,770,368 bytes |
| Highest sampled GPU board use | 20,560 MiB |

There were 115 samples across 336.408 seconds. The sampler sleeps one second but
includes nvidia-smi/collection overhead; this is not strict 1 Hz sampling. These
are observed sampled peaks and a kernel RSS high-water, not proof of a minimum
memory requirement. The actual cgroup allowance was much higher than 64/128 GiB,
so **this is not a hard-64-GiB or hard-128-GiB acceptance result**. Different counter
windows must not be treated as interchangeable whole-workload peaks. Native dynamic VRAM was enabled; Torch allocator counters do not include all native weight allocations. The [resource record](../artifacts/remote/60741609-20261115/resource-summary.json) states the counter and sampling scope.

The [rental ledger](../artifacts/remote/60741609-20261115/rental-ledger.json) separates all three attempts:

| Instance | Recorded outcome |
|---|---|
| 60679331 | Published 885 startup failed on template Python 3.12; failure preserved and instance destroyed. |
| 60740761 | Provider resources unavailable; no model download or daemon, instance destroyed. |
| 60741609 | Official-template H3 acceptance passed; instance destroyed. |

The successful instance and all other task instances were verified
absent through the official account API; stopping a container alone was not used
as proof of stopped billing. The observed account-credit decrease over the task
window was about **$0.6365826**. The compute/storage time × rate upper estimate for all three attempts is **$0.481584**, excluding traffic. These separate values do
not turn the observation into an itemized settled invoice. Account balances,
credentials, raw logs and private rental state remain local. The [publication manifest](../artifacts/remote/60741609-20261115/publication-manifest.json) explicitly separates the 17 selected artifacts from files retained only locally; it does not advertise the latter as public downloads.

## Historical 0.2.0 execution baseline

This is the **historical 0.2.0 execution baseline** preserved at
[758738e](https://github.com/Delva0/comani/tree/758738ee07e1d13223fde9013effd4fa6286f942).
Its scripts, exact build and commands describe that run. They do not validate
later CLI or native-option changes; use the [current startup guide](design/development.md)
for the supported product entrypoint. Raw artifacts below are unchanged.

**Passed on 2026-11-14 UTC. All rented instances were destroyed and verified
absent.** This report records tests performed by the independent remote
acceptance agent, rather than relying on the implementation owner's results.
The exact requested INT8 checkpoint produced real 2048×2048 images through
Comani eager, captured graph, and native API JSON execution. The three complete
float tensors are bitwise equal. The first default-memory attempt failed;
the successful run used native Comfy `--reserve-vram 4`.

### Actual output and runnable client

![Actual requested H3 INT8 output](../artifacts/remote/60662518-20261114/eager.png)

The actual executed client is [examples/minimax_h3_remote.py](../examples/minimax_h3_remote.py),
at `/home/delva/projects/comani/examples/minimax_h3_remote.py` after the project
directory migration. It reads `COMANI_URL` and `COMANI_TOKEN` from the environment.
It uses original node names and includes a real Python latent identity between
`KSampler` and `VAEDecode`; captured outputs are retained by an explicit eager
consumer. It also submits the equivalent unmodified Comfy API JSON.

```bash
python examples/minimax_h3_remote.py \
  --output artifacts/remote/my-run \
  --wheel-sha256 16a04c6a0afe494880c084039bffd1f02548d13e068c41240997f95972f005c6
```

Use a running, authenticated deployment with the exact manifest files; the
temporary acceptance endpoints have been shut down. The private endpoint/token
wrapper used during the test is the archived
[run_client.py model](https://github.com/Delva0/comani/blob/758738ee07e1d13223fde9013effd4fa6286f942/scripts/remote/run_client.py).

The [actual run record](../artifacts/remote/60662518-20261114/run.json) includes
the script path, prompt, seed 426190, 2048×2048 size, six steps, CFG 1,
Euler/simple, model hash, wheel hash, native reserve value and call IDs.
The [official guide](https://docs.comfy.org/tutorials/image/minimax/minimax-h3-image)
recommends this distilled six-step schedule; it is a normal model demonstration.

| Actual mode | End-to-end seconds | Max absolute float error versus eager |
| --- | ---: | ---: |
| Eager node → Python → node | 25.579 | 0 |
| Captured native graph → explicit eager retention | 34.983 | 0 |
| Native API JSON → explicit eager retention | 33.555 | 0 |

The successful run began at 16:04:05 UTC and finished at 16:05:41 UTC.
[image-comparison.json](../artifacts/remote/60662518-20261114/image-comparison.json)
records a second, independent comparison of the saved tensors with `torch.equal`,
including identical raw tensor SHA256
`38efaa6cb2da7f55332b1b9584db0359f1084ba563a4bfbaf00dd247d4e4c5d4`.
All PNGs have SHA256
`db9f84dd9885542f6d02e8f1224552c263dfb2258c5c64c7aa0e145aa46ab886`.
The image was visually inspected and is a detailed ceramic fox, rather than an
empty or corrupt output. [native_prompt.json](../artifacts/remote/60662518-20261114/native_prompt.json)
is the actual native reference submitted to the same native PromptExecutor.

One original 4,817,799-byte PNG is published. Duplicate `captured_graph.png`,
`native_api.png`, and all three complete float `.pt` files are retained locally
under `/home/delva/projects/comani/artifacts/remote/60662518-20261114/`.
They are not remote download links. The different `.pt` container hashes in
`run.json` do not indicate different tensor contents; their raw tensor bytes and
PNG bytes are identical.

### Exact build and model identity

The client and daemon used wheel `comani-0.2.0-py3-none-any.whl`, SHA256
`16a04c6a0afe494880c084039bffd1f02548d13e068c41240997f95972f005c6`.
All 15 Python files were independently compared between frozen source, archive
and actual installed client; the actual remote installation was separately
compared against the archive. See
[client-wheel-verification.json](../artifacts/remote/60662518-20261114/client-wheel-verification.json)
and the first case in [smoke/acceptance.json](../artifacts/remote/60662518-20261114/smoke/acceptance.json).

Actual remote runtime: Python 3.11.11, cloudpickle 3.1.2, torch 2.9.1+cu130,
comfy-kitchen 0.2.41, RTX 4090, driver 580.95.05, ComfyUI commit
`eee59185ff4f43ae2d9ad122b245619d27d9daad`. The ordinary package was installed
in `site-packages`; unpublished local source was not assumed to exist remotely.

The [requested Civitai version](https://civitai.com/api/v1/model-versions/3462708)
is version 3462708, model 3045867, file 3365259, uploaded by `tsolful`.
All three files were downloaded **on the rented machine**, with exact size and
SHA256 verification before generation:

| Component / filename | Bytes | SHA256 |
| --- | ---: | --- |
| Diffusion: minimax_H3_image_pruned_INT8.safetensors | 20,979,337,386 | 4647bdf553a32aa4ae55d9eb28322be42d32611d333c19a097d2845ae8a9b7a8 |
| Encoder: qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors | 15,687,142,551 | 35a88d51044231fe332301d7a62aa81e3f2cba62febeb446e2c1e3e0ef76f2c6 |
| VAE: minimax_h3_image_vae_fp16.safetensors | 1,168,505,312 | d80e9c2e5cfc9844869961ff5a58c9a9e52182df6bfb30b26018dd7f348721af |

See [model-verification.json](../artifacts/remote/60662518-20261114/model-verification.json)
and [models.json](https://github.com/Delva0/comani/blob/758738ee07e1d13223fde9013effd4fa6286f942/scripts/remote/models.json). Companion files are pinned to
[Comfy-Org revision 0bb149f51e3156ed4b90dd45d5620346bfe1df8f](https://huggingface.co/Comfy-Org/MiniMax-H3-Image/tree/0bb149f51e3156ed4b90dd45d5620346bfe1df8f).
The differently hashed Comfy-Org `int8_convrot` diffusion file and INT4/INT6 files
were not substituted. The optional prompt-expander was omitted because a detailed
prompt was supplied directly.

The third instance completed all hashes at 15:44:41 UTC. Diffusion took
1,401.375 seconds, encoder 343.883, VAE 76.780. Diffusion temporarily slowed to
about 0.61 MB/s, then recovered to 22.75 MB/s over the next measured 205 seconds.
A separate ordinary Range capability probe returned HTTP 403 before the stop
step, so the sole original writer was left running and completed successfully.
A normal local public API request reported Cloudflare error 1010. No challenge,
authentication or request-policy control was bypassed.

### SDK and transport acceptance

[acceptance.py](https://github.com/Delva0/comani/blob/758738ee07e1d13223fde9013effd4fa6286f942/scripts/remote/acceptance.py) passed all ten cases over the
actual local-client → HTTPS → remote-daemon route, on the final wheel:

- Actual GPU/build identity and handshake `bounded_wait=true`.
- Immediate eager submission, pending native/Python/native dependencies and
  explicit tensor fetch.
- A 0.382-second heartbeat while synchronous work occupied the native worker.
- Captured graph early intermediate refs, borrowed cache ownership, deliberate
  native-cache expiry, and explicit eager-owned output retention.
- Accepted consumer input retention across local GC, explicit alias release,
  upstream/dependency errors and subsequent worker recovery.
- Source-file execution, identical-content ordinary package reuse, changed-content
  package conflict, and reference invalidation on session close.

The disposable cache-eviction helper is a test action, not a new public SDK API.
See [the complete result](../artifacts/remote/60662518-20261114/smoke/acceptance.json).

[long_wait.py](https://github.com/Delva0/comani/blob/758738ee07e1d13223fde9013effd4fa6286f942/scripts/remote/long_wait.py) ran a real 130-second operation
through Cloudflare on the final build: 137.473 seconds end to end,
**one submit, seven bounded 20-second queries, one call ID, one remote execution**.
[long-wait.json](../artifacts/remote/60662518-20261114/long-wait.json) retains the
result. No timeout handler resubmitted the operation.

The independent agent also reran the eight native argument tests, including real
fresh-process Comfy parsing and explicit rejection of unknown/mutually exclusive
native flags. Those tests passed independently after the implementation freeze.

### Default OOM, native offload, and measured resources

The first exact-model 4MP eager attempt used the default Comfy reserve of 400 MiB.
It failed inside native INT8 linear: allocating 172 MiB with 157.75 MiB free,
21.76 GiB allocated and 1.09 GiB reserved. The error propagated correctly through
Comani; Comfy unloaded the models. The original full error and resource data are
retained locally under `artifacts/remote/60662518-20261114/attempt-default-oom/`.

The driver entrypoint used for this recorded run forwarded native Comfy
initialization arguments after `--`. The successful startup command was:

```bash
python -m comani._runtime --comfy-root /path/to/ComfyUI \
  --host 127.0.0.1 --port 8190 --no-custom-nodes -- --reserve-vram 4
```

The strong token was supplied through the environment, not the command text.
The real new PID was 13069. Parsed `args.reserve_vram` was 4.0,
`VRAMState.NORMAL_VRAM`, and `minimum_inference_memory()` was
5,153,960,755.2 bytes. This was a normal native memory-policy setting, not a
post-import global patch, custom model manager, lower precision, smaller image or
different checkpoint. [configured-daemon.json](../artifacts/remote/60662518-20261114/configured-daemon.json)
and [runtime-system.json](../artifacts/remote/60662518-20261114/runtime-system.json)
record the sanitized command/PID and actual native values.

Pinned [model_management.py](https://github.com/Comfy-Org/ComfyUI/blob/eee59185ff4f43ae2d9ad122b245619d27d9daad/comfy/model_management.py)
uses the reserve in its native model-loading/partial-offload budget. Default
UNet/text-encoder offload is CPU; the loader evicts models as needed.
The final loaded-model snapshot contains only the VAE on CUDA, with CPU as its
offload device. This snapshot is after generation, not a per-layer residency trace.
All model files were not assumed to coexist in VRAM.

| Measured quantity | Actual value |
| --- | ---: |
| Quoted RAM | 64,396 MiB |
| Actual cgroup-v1 RAM limit | 129,646,985,216 bytes = 120.75 GiB |
| Process RSS high-water, successful three-mode process | 74,153,021,440 bytes ≈ 69.06 GiB |
| Cgroup cumulative high-water | 82,377,285,632 bytes ≈ 76.72 GiB |
| GPU board peak sampled every second | 20,960 MiB |
| Torch maximum allocated / reserved | 20,050,215,936 / 21,369,978,880 bytes |
| Actual filesystem capacity | 75,161,927,680 bytes = 70 GiB |
| Final disk used / free | 44,817,641,472 / 30,344,286,208 bytes |

See [resource-peaks.json](../artifacts/remote/60662518-20261114/resource-peaks.json),
[cgroup.json](../artifacts/remote/60662518-20261114/cgroup.json), and
[runtime-system.json](../artifacts/remote/60662518-20261114/runtime-system.json).
One-second raw samples and full logs remain local. The sampler's cgroup-v2 field
is zero because this host used cgroup v1; the separate v1 records supply the real
limit/high-water. The reported process peak is kernel-tracked as well as sampled.

**This is not a hard-64-GiB pass or proof of minimum RAM/disk.** The quote and
actual enforced limit differ, and the three-mode process exceeded 64 GiB RSS.
At least 80 GiB of RAM allowance is a practical starting suggestion for repeating
this particular acceptance workload, not an experimentally established minimum.
70 GiB disk was sufficient, with 28.26 GiB remaining; smaller disks were not tested.

### Actual Ada NVFP4 support

Native NVFP4 compute requires NVIDIA compute major >=10 in pinned Comfy.
RTX 4090 is Ada SM 8.9, so it uses the native packed-weight/dequantization fallback
in [ops.py](https://github.com/Comfy-Org/ComfyUI/blob/eee59185ff4f43ae2d9ad122b245619d27d9daad/comfy/ops.py),
rather than a Blackwell-only FP4 matrix kernel.

The actual 4090/cu130 diagnostic used a packed 128×256 layer: native FP4 false,
fallback true, permanent parameter on CPU, BF16 output. The layer exactly matched
same-CUDA-backend dequantization plus BF16 linear: max absolute error 0, ULP 0.
CPU versus GPU dequantization differed by at most 0.015625, relative 0.0072993,
one BF16 ULP. CUDA TF32 matmul was false; float32 precision was `highest`.
See [nvfp4-diagnostics.json](../artifacts/remote/60662518-20261114/nvfp4-diagnostics.json).
The actual full-size encoder also produced finite `[1,84,5120]` conditioning and
`[1,24,1,128,128]` latent in 7.384 seconds before diffusion testing.

An earlier startup probe incorrectly required CPU/GPU dequantization to match
bitwise. That failure is retained locally in the earlier instance's artifacts.
The corrected oracle compares the same GPU computation; it did not loosen the
tolerance. Full-model success is established separately by the three real images.

### Rental cost and verified cleanup

Only this task's instances were managed. The official CLI used the user's normal
local credential configuration; the Vast account API key was never printed or
sent to a rented machine. No top-up or recurring purchase was made.

| Instance | UTC created | UTC destroyed and verified absent | Compute + storage USD/hour |
| --- | --- | --- | ---: |
| 60653211, Netherlands | 13:48:41 | 13:58:55 | 0.479259 |
| 60654415, Spain | 14:01:10 | 14:58:24 | 0.481852 |
| 60662518, Sweden | 15:18:56 | 16:09:22 | 0.431444 |

The successful instance rate comprised compute $0.412/hour and storage
$0.019444/hour, ingress $0.00260417/GB and egress $0.00390625/GB. Its
compute/storage rate-time upper estimate is $0.362690. All three rate-time
estimates total **$0.904012**, excluding traffic and billing-category differences.
At 16:10:27 UTC, the observed account-credit change showed about **$1.1804
deducted** for this task. Account balances remain in the private local record.
This observed credit change is not an itemized settled invoice.

[rental-ledger.json](../artifacts/remote/60662518-20261114/rental-ledger.json)
records all rates, traffic prices, timestamps and verified absence. At
16:10:23 UTC the official account instance list was empty. The local watchdog
exited, and both temporary tunnels returned HTTP 530/disconnected. The original
18:40 UTC destruction deadline was preserved through the directory migration;
normal completion destroyed the instance much earlier. Container shutdown alone
was never treated as proof of stopped billing.

### Deployment lessons and scope

Earlier SSH routes established TCP but returned no SSH banner despite sshd
listening in the container. System proxy/firewall/VPN settings were left alone.
The restricted provider `execute` endpoint was not converted into a shell.
The final deployment used the documented Docker entrypoint/args route.

An onstart metadata ACK did not establish actual execution. One recycle lost the
container filesystem; a reboot retained it but still ran the older wrapper's
startup file. Those observations do not establish a provider bug. The successful
explicit args launcher printed a unique run ID and payload/landed-source hashes,
which were checked before real authenticated requests.

The working deployment separates environment/wheel/daemon readiness from model
download and optional probes. A diagnostic failure cannot kill its control plane;
the fixed resume actions are stage names, not arbitrary shell commands. Download
budgeting subtracts existing partial bytes and verifies complete files. The later
reserve configuration used an explicit audited restart helper and the genuine
project CLI, without recreating the container or downloading weights again.

For ordinary deployment, use the [current startup instructions](design/development.md)
with a deployment-provided Comfy environment and the lightweight daemon command.
Environment versions, models and network access are deployment choices.
The rental controller and dual tunnels
are acceptance infrastructure, not required product services. Native runtime
arguments should remain a thin pass-through; custom native nodes do not require
SDK namespace regeneration or a separate communication plugin.

The temporary TLS/token fallback used Cloudflare Quick Tunnels. Its
[official limits](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)
include 200 in-flight requests, no SSE, changing hostnames and no uptime guarantee.
The [125-second read timeout](https://developers.cloudflare.com/fundamentals/reference/connection-limits/)
was crossed by a real operation using bounded polling. Local high-concurrency
stress results are not claimed as proxy capacity tests.
