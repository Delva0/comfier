# Independent RTX 4090 acceptance

Status: **not yet passed**. This report distinguishes source research, actual
local operator testing, provider/container observations, and full remote model
execution. It must not be read as a completed remote model test.

## Requested weights and reproducible inputs

The requested [Civitai model version 3462708](https://civitai.com/api/v1/model-versions/3462708)
is `Minimax H3 IMAGE INT8`, model `3045867`, file `3365259`,
`minimax_H3_image_pruned_INT8.safetensors`, 20,979,337,386 bytes. Its required
SHA256 is `4647bdf553a32aa4ae55d9eb28322be42d32611d333c19a097d2845ae8a9b7a8`.
INT4/INT6 and the differently hashed Comfy-Org INT8 file are not substitutions
for this acceptance.

The [model author's requirements](https://civitai.com/api/v1/models/3045867)
identify the H3 Qwen3-VL encoder, including the NVFP4 AWQ variant, and the image
VAE. The video VAE is not compatible. The three exact URLs, sizes, and SHA256s
are in [models.json](../scripts/remote/models.json). Companion weights are pinned
to Comfy-Org HF revision `0bb149f51e3156ed4b90dd45d5620346bfe1df8f`.
The optional prompt expander is omitted; a detailed prompt is supplied directly.

The [official image guide](https://docs.comfy.org/tutorials/image/minimax/minimax-h3-image)
specifies six Euler/simple steps and CFG 1. Its low-VRAM starting canvas is about
4MP. The planned full generation test uses 2048x2048, six steps, fixed seed,
native `MiniMaxH3ImageConditioning`/`KSampler`/`VAEDecode`, and compares eager
node/Python/node output with raw native API prompt output. Tiny image calls only
test API semantics and do not count as this model generation test.

## Offload and NVFP4 evidence

Pinned ComfyUI: `eee59185ff4f43ae2d9ad122b245619d27d9daad`.
The local checkout reports this HEAD and no tracked modifications.

In [model_management.py](https://github.com/Comfy-Org/ComfyUI/blob/eee59185ff4f43ae2d9ad122b245619d27d9daad/comfy/model_management.py),
`supports_nvfp4_compute` requires NVIDIA compute major >=10. Ada GPUs have no
native FP4 matmul. In [ops.py](https://github.com/Comfy-Org/ComfyUI/blob/eee59185ff4f43ae2d9ad122b245619d27d9daad/comfy/ops.py),
unsupported formats select full-precision matrix multiplication while preserving
packed parameters. The layer casts/offloads its weight and dequantizes at
forward time. Default UNet/text encoder offload targets are CPU; native loading
evicts lower-priority models when GPU space is needed. Summing every model file
as simultaneously resident VRAM would be an incorrect feasibility test.

Actual operator probe: Python 3.11.11, torch 2.9.1+cu128, comfy-kitchen 0.2.41,
RTX 4060 Laptop (Ada SM 8.9). A packed NVFP4 `128x256` layer stayed as CPU
`uint8[128,128]`, executed through Comfy's emulated GPU path, and exactly matched
explicit dequantized linear output; peak CUDA allocation was 8,609,280 bytes.
[probe_nvfp4.py](../scripts/remote/probe_nvfp4.py) reproduces the probe. This is
actual operator evidence on Ada, **not** evidence that the full model ran on the
rented RTX 4090. The same probe and full generation still need remote execution.

The remote plan uses torch 2.9.1+cu130 because this Comfy version warns that
optimized comfy-kitchen CUDA operations require cu130. The permanent packed
diffusion/encoder/VAE files total about 35.24GiB. A quoted 64GB RAM/70 storage-unit
offer is a candidate, not a measured minimum. Actual filesystem size, free RAM,
cgroup limits, driver, peak RSS/VRAM, and offload logs must be recorded before
making a smaller-memory claim.

## Rental ledger and transport observations

Only this task's instances are managed. No existing instances were present at
the first account check. The local official Vast CLI reads the user's configured
key; the key is never printed or copied to the rented container. No top-up or
reserved/recurring purchase was made.

| Instance | UTC start | UTC verified destroyed | GPU+storage USD/hour | Network ingress/egress USD/GB | Outcome |
| --- | --- | --- | --- | --- | --- |
| 60653211, Netherlands | 2026-11-14 13:48:41 | 2026-11-14 13:58:55 | 0.479259 | 0.00390625 / 0.00390625 | sshd started; no SSH banner through direct or Vast proxy; destroyed and verified absent |
| 60654415, Spain | 2026-11-14 14:01:10 | **still active while fallback decision is pending** | 0.481852 | 0.00260417 / 0.00390625 | sshd started; both transport paths time out before authentication |

First instance compute/storage estimate upper bound: $0.081784 over 0.170646h.
This is a rate/time estimate, not a settled invoice. No model bytes were downloaded
on either instance yet. Container image setup may have separately billed traffic;
final invoices remain to be checked. The requested disk value was API `70`; actual
`df` has not been obtainable and is not asserted to be 70GiB.

The second instance's local watchdog deadline is 2026-11-14 18:46:10 UTC.
Its PID, label, rate, and instance ID are in ignored local `rental.json`; every
30 seconds it records status. Normal completion destroys and verifies absence
much earlier. A remote `shutdown` is not used as proof of stopped billing.

Provider logs on both machines report `sshd` listening on IPv4/IPv6 port 22.
Vast's direct and proxy endpoints reach TCP establishment but provide no SSH
banner. Independent Windows socket reads agree with WSL. Windows has an existing
local HTTP proxy on `127.0.0.1:10808`; Windows HTTP CONNECT returned 200 but no SSH
banner. WSL cannot reach that Windows loopback listener. No system proxy, VPN,
firewall, route, browser security, or authentication setting was changed.

Computer Use could not inspect the current Civitai browser tab because its URL
was refused by that tool. Browser work stopped; no cookie/secret export or URL
policy workaround was attempted. This does not establish Vast browser login
status. CLI authentication was subsequently configured by the user and verified.

## Prepared acceptance and deployment

[bootstrap.sh](../scripts/remote/bootstrap.sh) pins the Comfy revision and
Python/cloudpickle ABI; [download_models.py](../scripts/remote/download_models.py)
downloads on the rented machine, resumes partial files, verifies size/SHA256,
and rejects authentication/hash failures. [hardware_preflight.py](../scripts/remote/hardware_preflight.py)
records actual container resources before large downloads.

[acceptance.py](../scripts/remote/acceptance.py) is the independent local SDK test:
real remote GPU metadata, eager future chaining, driver responsiveness during
synchronous compute, captured graph intermediate readiness, explicit eager
retention, borrowed-cache expiry, GC input handoff, alias release, errors and
recovery, source/package reuse/conflict, session close, and full model comparison.
It has not yet run remotely. Native-cache eviction in this disposable acceptance
daemon is an explicit test helper, not a new public SDK method.

Final acceptance must rebuild the frozen SDK/runtime wheel, record its SHA256,
and test those exact bytes after the ongoing lifecycle fix. Earlier source
archives are not evidence for the final build.

Deployment should stay small: one official rental command, one pinned Comfy
checkout, one package/wheel, a three-file model manifest, native offload, and a
loopback daemon reached by SSH tunnel. New custom nodes use the native registry;
no SDK namespace regeneration or separate communication plugin is needed.

## Proposed HTTPS fallback: not activated

Vast's [documented SSH launch mode](https://docs.vast.ai/guides/instances/connect/overview.md)
supports an onstart script as its normal entrypoint. The official CLI's
`update instance --onstart` updates/recreates that owned instance through a
separate deployment API. It does not encode arbitrary commands into the limited
`execute` file-management endpoint. The latter's rejected command is left
rejected; no injection or whitelist bypass is attempted.

A possible fallback is normal onstart deployment and a temporary, strong-token
HTTPS endpoint through Cloudflare Quick Tunnel. It has not been submitted or
exposed. [Quick Tunnel limitations](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)
include 200 in-flight requests, no SSE, changing hostnames, and no uptime guarantee.
Cloudflare supports WebSocket proxying. Its [current connection limits](https://developers.cloudflare.com/fundamentals/reference/connection-limits/)
state a 125-second proxy read timeout and 30-second write timeout. Current
single-request `wait_result` can exceed this; an unchanged SDK is not claimed to
work for long H3 jobs through that channel. Correct support needs bounded wait
responses with repeated read-only queries for the same call ID, never resubmission.
Local high-concurrency stress results must not be relabeled as Quick Tunnel tests.
