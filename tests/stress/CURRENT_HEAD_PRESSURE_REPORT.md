# Current-installation CPU pressure acceptance

**Final v4-r2 independent acceptance: passed within the measured CPU/HTTP boundary.** Live public Call/Future/Ref handles now retain their Client; the last user owner may disappear without an internal task, queued callback or copied Context silently keeping Client alive. Explicit close overrides these owners. All previous baseline, v2, v3, HTTP-only r3 and first shared-owner v4 evidence remains separately retained. Compact source-bound results and raw-evidence hashes are in [CURRENT_HEAD_PRESSURE_RESULTS.json](CURRENT_HEAD_PRESSURE_RESULTS.json).

## Accepted shared-owner candidate

Final wheel SHA256: `64a73a6e6a3c40ca6435c521bb9d7df4411e9e646c45334c863796b741babd98`. Production manifest SHA256: `0b12ee002fb774911ee64604bbff1c1b761ce2c668b66508a2d4dab9956249a2`. The wheel archive, all **17 actually installed package Python files**, and frozen production checkout match the manifest. Each SDK process records actual loaded module paths and hashes before/after its workload. Tests use the Torch-free installed SDK at `/home/delva/projects/comani/.local/independent-heartbeat/v4-r2/sdk311`, not imports from the checkout.

The external native CPU server uses the compatible baseline installation. Its unchanged runtime files, including `service.py` SHA256 `0b95ca36c99e80908779e07013775b402f70c1ddb6cb4c57170e9c5a4c607ea9`, match the final candidate. ComfyUI remains `eee59185ff4f43ae2d9ad122b245619d27d9daad`. SDK ownership is measured in a different process from native execution. Python 3.12 daemon/startup acceptance and the ordinary suite have separate owners; these tests do not relabel the baseline server's older startup module as the new one.

| Final r2 check | Independently observed result |
|---|---|
| Public ownership | Helpers return only pending Call, pending Future or ready Ref plus weakrefs/strings. After the local Client variable disappears, all three retain Client and fetch correct values. Last handle drop then collects Client/session. |
| Aliases and release | Last remaining Future alias retains Client. Releasing a cell invalidates all three Future/Ref aliases and removes that cell's Client hold even while released aliases remain held. A retained Call independently keeps Client alive; a separate valid sibling output cell also continues to retain it. |
| Explicit close and async-with | Close takes precedence over retained handles. Remote use raises SessionClosed; repeated close is safe. Call has no explicit release operation: its ownership ends with Call GC. |
| Pending internal watcher | After submit ACK, an actual HTTP wait response is held indefinitely in the external test server. With no public handle left, the internal watcher does not retain Client; session, call, object, thread and connection cleanup converge. |
| Non-empty user Context | A Client binding is explicitly reset before GC. SDK start/await preserves the live user binding, while copied internal task/Future/watcher Context does not retain the former Client. Separate queued release, queued forget and cross-Client GC Context groups all pass. |
| Queued controls before loop progress | With the last owner released/dropped, Client weakrefs become None immediately. Blocking the business loop for over three TTLs produces no later heartbeat and the unowned session expires. HTTP disposal converges once the loop is allowed to progress. |
| Borrowed workflow output | The factory's Graph, symbolic nodes and run Call all leave scope. The borrowed Ref alone retains the session; real native cache eviction still makes fetch raise Expired. Shared Client ownership does not pin or revive the native cache. |
| Business-loop CPU block | Four sessions keep correct owned outputs through a 3.303-second synchronous Python busy loop at TTL 1.5 seconds. |
| Large complex workflows | The original stage is replayed against this final manifest: eight sessions, 32 graphs with 224 declared nodes each, 64 invocations, native/Python/nested inputs/composition/lazy/expansion/two output aliases. All 32 owned results equal input + 448; 286 actual awaits and 64 cancelled waiters, 54.138 seconds. |
| 32-session pressure | 4,096 accepted/completed calls, 18,412 actual awaits and 4,096 cancelled waiters in 25.024 seconds. Every session is still active with calls/objects zero at public close entry. All local states/watchers clear; no business RPC error or active-lease expiry occurs. |
| 1,000-session churn | TTL 0.3 seconds, batches of ten; 1,000 actual native calls/fetches pass in 13.204 seconds. At 100/300/1,000 sessions and another 14 TTLs, active sessions/calls/objects/requests are zero and diagnostics stay at 64. The only expiry is the separately minted intentional idle session. |
| Lifecycle and startup failures | Terminal server rejection never revives a session or resubmits business work. Owning-loop closure stops the keeper. Original failed-open and cancelled-handshake probes pass; no extra keeper threads remain and SDK socket FDs return to the identical baseline set. |

The final pressure run records one **closing** TTL fallback, not an active-owner failure: close begins at monotonic 8182.880406 while the session is active and calls/objects are already zero; expiry is at 8187.699595, the idempotent close RPC reaches native at 8187.749592, and close succeeds at 8188.011589. GC is proven before that expiry. The separate no-owner blocked-loop cases intentionally stop renewal and allow TTL cleanup while the business loop cannot yet send close. There is no blanket zero-expiry claim across these workloads.

Pressure RSS peaks are **1,041.41 MiB native server** and **176.88 MiB SDK client**. Largest observed business-loop delay is 2,069.072 ms, exceeding the configured TTL, while held public owners remain usable. These are separate process measurements; no daemon/observer RSS is presented as cgroup memory. The large-graph stage briefly overlaps one small old-v4 Context counterexample in separate processes; it is correctness evidence, not a maximum-throughput benchmark.

## Preserved counterexamples and observation limits

- HTTP-only r3 had weak Client ownership: pending Call, pending Future and ready Ref all fail after their helper-local Client disappears. Final r2 passes the same three factory shapes.
- First shared-owner v4 passes ordinary ownership groups but queued bound release/forget callbacks retain Client with no remaining user owner. Two real blocked-loop cases record 8/9 later heartbeats. Final r2 weak dispatch passes the identical cases.
- Old v4 also retains Client through a copied non-empty user Context after the binding and all public handles are cleared. The same hung-wait case passes on r2. Observers do not retain a dereferenced weakref or a factory-local Graph/Call.
- One first r2 observer attempt asserted socket-FD equality before allowing the resumed business loop to execute HTTP disposal. Its ownership checks already passed. That output remains saved; the corrected observer explicitly yields and waits for final FD convergence, in a separate result file.

The final raw directory is `/home/delva/projects/comani/.local/shared-owner-acceptance-20261115T034719Z/`; `v4-r2/final-summary.json` and the compact public JSON bind the evidence to the final wheel. Earlier evidence remains in its original directory and `prior-v3-public/`. Own process/port cleanup is verified, including all ports 18400-18414. No remote account, GPU, model file, unrelated process or production source was changed by this acceptance agent; no commit or push was made. Six public probes pass Ruff.

The original 140-second one-submit bounded-wait and saturated wait-pool results below remain evidence for unchanged runtime/wait/control mechanisms; they were not wastefully repeated. They do not stand in for the final shared-owner, Context, large-graph, pressure or churn runs above. No multi-hour endurance, WAN recovery, GPU throughput/VRAM ceiling or broad custom-node compatibility claim is made.

## Retained v3 acceptance under the previous ownership contract

The following v3 observations retain their historical scope. Its test expecting Client GC while a Ref remains live is **not** acceptance of the subsequently selected shared-ownership contract.

### Historical v3 frozen candidate

The accepted v3 wheel is `comani-0.3.0-py3-none-any.whl`, SHA256 `26213fe2c84fda1e7ad44dfd991424e8343d24a28654e9723ca7d3febc868fdc`; its production manifest SHA256 is `28f848e63d9a89a3a4208b8537f82fd8a2304b31a44510c876622c9dea0373b3`. All **17 package Python files**, the wheel archive, actual installed SDK files and frozen production checkout match that manifest. Actual loaded-module origins and hashes are retained before and after each run. The SDK is the separate Torch-free environment `/home/delva/projects/comani/.local/independent-heartbeat/v3/sdk311`.

The native HTTP server is a separate process using the unchanged baseline installation and real CPU ComfyUI. All unchanged native runtime files, including `service.py`, are checked against the candidate. Its older startup module is identified in the evidence: these tests accept the SDK heartbeat/lifecycle change against that compatible native server, not a new Python 3.12 daemon startup. Python 3.12 startup and the broader standard suite have separate acceptance owners.

| Final v3 check | Independently observed result |
|---|---|
| Owned ref while business loop is blocked | TTL 1.5 s, real synchronous sleep 2.204 s; the same ref fetches the correct value, one producer submission, no expiry. |
| Four sessions during synchronous CPU work | A 3.302 s Python busy loop blocks the SDK business loop; all four owned refs remain usable. |
| 32-session pressure at the original short TTL | 4,096 accepted/completed calls, 18,480 actual awaits, 4,096 in-progress waiter cancellations; 24.635 s, zero RPC errors and zero active-lease expiry. Every session is still active with calls/objects zero at its public close entry. All local states/watchers and extra keeper threads clear. |
| 1,000-session keeper churn | Same TTL 0.3 s and batches of ten: all 1,000 native calls/fetches pass in 13.422 s. At 100/300/1,000 sessions and after another 14 TTLs, active sessions/calls/objects/requests are zero and diagnostics stay at 64. No extra keeper threads remain. |
| Intended idle expiry | The extra session deliberately created without a heartbeat is the only churn expiry, at seen-age 0.347623 s. No active churn session expires. |
| Lifecycle | Ref GC with Client alive, Client GC with a retained ref, repeated close, terminal server rejection without session revival or business resubmission, and owning business-loop closure with Client/ref still retained all pass. |
| Failure/cancellation cleanup | The original failed-open and gated-handshake-cancellation probes pass unchanged. Their independent outer audit observes zero extra threads and zero socket FDs after `asyncio.run` returns. The lifecycle process returns to its identical baseline socket-FD pair. |

The final 32-session process-separated RSS peaks are **1,040.39 MiB native server** and **176.45 MiB SDK client**. The SDK business loop's largest sampled delay is 2,202.222 ms, exceeding the configured TTL; active owned refs nevertheless survive. Native-server loop delay peaks at 278.628 ms. These observations are not capacity maxima, latency guarantees or cgroup memory measurements. Zero RPC errors means observed business-transport failures and native HTTP error responses; background transient retry counts are not measured.

The v2 candidate exposed a second real defect during churn: after 20 successful sessions, all 30 native producers in the next batch completed and every session remained active, but `fetch()` was locally rejected by `RemoteError: Session heartbeat unavailable: ` before a fetch RPC was sent. There was no server RPC error or expiry. V3 restricts local invalidation to confirmed terminal lease errors; the identical churn workload now completes all 1,000 sessions. The original v2 JSON and traceback remain retained.

## Closing TTL fallback is distinct from active lease failure

An instrumented v2 32-session run recorded one TTL cleanup **after** the user had begun closing an already empty session. Public close entry was at monotonic `2362.215281`, with the session active and calls/objects both zero. Heartbeats continued through `2366.134431`; expiry followed at `2368.074835`, the explicit close RPC arrived at `2368.418767`, and close completed successfully at `2368.981398`. Thus expiry did not supply the earlier GC result or invalidate a still-active owned value. Background renewal stops during explicit shutdown; TTL remains a cleanup fallback while shutdown RPC progress is delayed.

The first v2 pressure run lacked close-entry observations and is retained with that limitation. The final v3 pressure run records every close entry and has no expiry at all. This report does **not** make a blanket zero-expiry claim across historical runs.

## Provenance and reason for rerunning

The tested published baseline is commit `885b8cd4709608f0e624d1e1f63b897115528b81`, version `0.3.0`, wheel SHA256 `bb117cd54002d794e90d0ce6d7dcf3d647efa18cf80f32339b8be1aeece6ed3a`. The actual package is `/home/delva/projects/comani/.venv/lib/python3.11/site-packages/comani`, Python 3.11.11. Native ComfyUI is `eee59185ff4f43ae2d9ad122b245619d27d9daad`.

All 17 installed package Python files match the v2 production manifest and wheel archive. Each process records installed files and the actual loaded module paths/hashes, before and after its probe. In particular, the baseline loaded `client.py` is `365402325f13275e5514798e7f37e8cee2d9d4ef29d19ca585f8e87a04c7c7db`, `service.py` is `0b95ca36c99e80908779e07013775b402f70c1ddb6cb4c57170e9c5a4c607ea9`, and `startup.py` is `db6a408be10aa4a122fa179c78a1f32eeb318c0674a11e379f4d604bde210c9d`.

The historical [load report](INDEPENDENT_LOAD_REPORT.md) lists a different `service.py` hash, `a9dba0f6…`; it is not byte-for-byte provenance for this installed baseline. The new native initialization and metadata work also justify checking the actual installation. Later checkout-only Python 3.12/startup edits are recorded separately by the observer; these baseline processes continue to load the unchanged installed wheel. This is not acceptance of an unpublished CLI candidate.

## Observed baseline results

| Check | Actual result |
|---|---|
| Original seven-phase probe | First six phases pass. The 32-session phase completes all 4,096 native calls, enters 18,378 awaits and cancels 4,096 waiters, but one output check raises `SessionClosed`; process exits 1. |
| Large composed workflows | Eight sessions, 32 workflows, 224 declared nodes each; 64 graph/owner invocations complete in 58.754 s. Every owned result equals input + 448. Native/Python, nested captured graphs, nested inputs, lazy failing branches, expansion, aliases and fetched owned refs are covered. |
| Default 120 s TTL, isolated HTTP processes | 32 sessions × 128 calls = 4,096 accepted/completed; 18,400 actual awaits and 4,096 cancellations. All surviving output/value checks pass in 23.943 s. Zero RPC errors and zero session expiry. Every session's call/object registry clears before its explicit close; all 32 closes come from RPC. |
| Original 0.3 s TTL churn | Fails with `SessionClosed` in submit while awaiting an output. The final results JSON was never produced. The failure stack, process exit and source provenance remain saved. |
| Isolated 0.3 s TTL churn | 1,000 sessions/calls pass in 11.771 s, in batches of ten. At 100/300/1,000 sessions and after another 14 TTLs, active sessions/calls/objects/requests are zero and diagnostic records stay at 64. All 1,000 closes are explicit; a separate idle session expires at seen-age 0.313982 s, as intended. |
| Real 140 s bounded wait | 140.010 s through a 125 s upstream-read-cutoff proxy; exactly one submit, six pending responses and ten bounded query/terminal rereads. Sixteen local waiters, four cancelled; surviving refs agree. Calls/objects/SDK states/watchers clear before close. |
| Saturated wait pool | All 128 calls accepted while the native producer is unfinished; wait connector holds 100 connections, control connector 0. Heartbeat 0.467 ms, close(wait=False) 19.871 ms. All three HTTP pools, watchers and runtime records close. |
| Held owned ref with blocked business loop | **Reproduced limitation:** one owned ref and its Call remain strongly held; a real 2.275 s business-loop sleep exceeds TTL 1.5 s. The independent server expires at seen-age 1.918577 s with one call and no active request. Resumed `fetch()` raises `SessionClosed`; the producer executed once. |

The default-TTL isolated load reuses the existing `load_session`, `make_graph`, `assert_output` and waiter counter. The counter increments inside the running waiter immediately before `await`; these are not counts of tasks merely constructed. A test-only read-only route returns actual native registries for the original GC assertions. SDK and native server are separate processes; no production lifecycle method is changed by the observer.

## What the 1.5 s diagnostic actually proves

The same 32-session load with an isolated native server and TTL 1.5 s verifies all values (4,096 calls, 18,442 awaits, 4,096 cancellations), but also records **31 background heartbeat failures after 31 real session expiries**. Twenty-nine of those sessions still have 128 completed call records at expiry. Therefore that run's empty registry cannot be claimed as proof that SDK GC alone reclaimed it.

For the first observed expiry, the server's last successful heartbeat is at monotonic 347.146646 s. The client then records a 1.424681 s event-loop delay; its next heartbeat begins at 349.057163 s, but the server receives it only at 349.841361 s. The server has already expired that session at 349.339467 s, with seen-age 1.889724 s and no tracked active request. The next heartbeat is rejected. This supports an actual lease timeout, rather than an unexplained early expiry. The original shared-loop run stored only exception strings, so its one failed output check has not been individually attributed to a particular RPC by this later trace.

Baseline heartbeat and sender tasks share the SDK business loop. The baseline client interval is `min(20, max(.01, TTL / 3))`; server sweep interval is `min(10, max(.02, TTL / 3))`. Keeping an owned local reference does not itself send traffic. This explains why the baseline can lose its session after a blocked loop exceeds TTL without an active HTTP read. The requested independent HTTP renewal is accepted above using the same TTL, rather than by raising it. InProcessTransport cannot prove service-loop isolation; the blocking tests use a real external HTTP server.

## Measurement scope and evidence

The original seven-phase and 140-second probes combine SDK, HTTP server, native runtime and collector in one process; their RSS is **combined harness RSS**, not isolated daemon memory. The process-separated observers record native-server and SDK-client RSS separately. For the 1.5 s diagnostic, their measured peaks are 1,040.94 MiB and 171.14 MiB respectively. The default-TTL client's largest observed loop delay is 1,740.995 ms, while native-server delay is 313.262 ms. These are CPU small-object measurements, not GPU throughput, maximum capacity, multi-hour stability or a memory ceiling. This WSL environment exposes no usable `memory.current`; no process RSS is presented as cgroup memory.

Raw scripts, JSON, tracebacks, logs, before/after loaded-source manifests, PIDs and port cleanup are retained at `/home/delva/projects/comani/.local/current-pressure-20261114T180558Z/`. `baseline-summary.json` preserves the mixed result and SHA256 of the raw JSON evidence. Key files are:

- `independent_load_results.json`, `independent_load-provenance.json`, `independent_load-process.json`, `independent_load.log`.
- `isolated_ttl120{,-server}.json`, `isolated_ttl1p5{,-server}.json`.
- `session_churn_probe.log` and its provenance/process records; `isolated_churn_ttl0p3{,-server}.json`.
- `bounded_wait_results.json` with the full one-submit RPC trace; `connection_pool_results.json`.
- `blocked_loop_baseline{,-server}.json` and `blocked_loop_probe.py`.
- `pause_cleanup.json` and `resumed_baseline_cleanup.json`.

All test-created native/proxy/loop-worker processes have exited and been reaped; their own ports are checked after each run. Final v3 logs and JSON are under the `heartbeat-v3/` evidence subdirectory. Baseline, v1 and v2 artifacts remain separate, including the original churn run that never reached its final JSON. No unrelated process, remote GPU/account, model file or product source was changed by this acceptance agent. No commit or push was made. The ordinary regression suite was not redundantly rerun by this agent; the implementation and Python 3.12 acceptance owners cover those checks.

## Reproduce the final lease checks

The six public probes are [shared owners](shared_owner_probe.py), [queued controls/Contexts](queued_owner_cleanup_probe.py), [blocked-loop](blocked_loop_probe.py), [isolated pressure/churn/large graphs](isolated_load_probe.py), [lifecycle](lease_lifecycle_probe.py), and [startup-failure/cancellation audit](lease_startup_cleanup_probe.py). They load installed packages; the supplied manifest pins every package file. From the repository root in the retained local environment:

```sh
PRESSURE_SDK=.local/independent-heartbeat/v4-r2/sdk311/bin/python
PRESSURE_MANIFEST=.local/shared-owner-acceptance-20261115T034719Z/v4-r2/production-manifest.json
NATIVE_MANIFEST=.local/thin-cli-review-20261114T170741Z/production-manifest-v2.json
PRESSURE_OUT=.local/new-pressure-reproduction
mkdir -p "$PRESSURE_OUT"
"$PRESSURE_SDK" -I -B tests/stress/shared_owner_probe.py --manifest "$PRESSURE_MANIFEST" --server-manifest "$NATIVE_MANIFEST" --port 18324 --output "$PRESSURE_OUT/shared.json"
"$PRESSURE_SDK" -I -B tests/stress/queued_owner_cleanup_probe.py --manifest "$PRESSURE_MANIFEST" --server-manifest "$NATIVE_MANIFEST" --port 18325 --output "$PRESSURE_OUT/queued.json"
"$PRESSURE_SDK" -I -B tests/stress/queued_owner_cleanup_probe.py --contexts --manifest "$PRESSURE_MANIFEST" --server-manifest "$NATIVE_MANIFEST" --port 18326 --output "$PRESSURE_OUT/contexts.json"
"$PRESSURE_SDK" -I -B tests/stress/blocked_loop_probe.py --manifest "$PRESSURE_MANIFEST" --server-manifest "$NATIVE_MANIFEST" --port 18320 --output "$PRESSURE_OUT/blocked.json"
"$PRESSURE_SDK" -I -B tests/stress/lease_lifecycle_probe.py --manifest "$PRESSURE_MANIFEST" --server-manifest "$NATIVE_MANIFEST" --port 18321 --output "$PRESSURE_OUT/lifecycle.json"
"$PRESSURE_SDK" -I -B tests/stress/isolated_load_probe.py --manifest "$PRESSURE_MANIFEST" --server-manifest "$NATIVE_MANIFEST" --ttl 1.5 --port 18322 --sessions 32 --output "$PRESSURE_OUT/load.json"
"$PRESSURE_SDK" -I -B tests/stress/isolated_load_probe.py --manifest "$PRESSURE_MANIFEST" --server-manifest "$NATIVE_MANIFEST" --mode churn --ttl .3 --port 18323 --output "$PRESSURE_OUT/churn.json"
"$PRESSURE_SDK" -I -B tests/stress/isolated_load_probe.py --mode graphs --manifest "$PRESSURE_MANIFEST" --server-manifest "$NATIVE_MANIFEST" --ttl 120 --port 18327 --output "$PRESSURE_OUT/graphs.json"
"$PRESSURE_SDK" -I -B tests/stress/lease_startup_cleanup_probe.py "$PRESSURE_MANIFEST" "$PRESSURE_OUT/failed-open-audit.json" failed_open_probe --output "$PRESSURE_OUT/failed-open.json"
"$PRESSURE_SDK" -I -B tests/stress/lease_startup_cleanup_probe.py "$PRESSURE_MANIFEST" "$PRESSURE_OUT/startup-cancel-audit.json" startup_cancel_probe --output "$PRESSURE_OUT/startup-cancel.json"
```

Use `--expect-expiry` on the blocked-loop probe with the original baseline SDK to reproduce the preserved negative result. Occupied ports are refused; no existing service is stopped. Choose a new output directory so earlier failed evidence is retained.
