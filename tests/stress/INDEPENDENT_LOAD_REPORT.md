# Independent native HTTP load acceptance

Raw log filenames below refer to files retained locally in this directory; the published repository includes the selected JSON evidence.

**Independent local acceptance: passed within the measured CPU/native boundary.** All local defects found during this acceptance were repaired by the implementation owners and then independently rerun with their original reproductions. The final frozen source also passes the new real 140-second bounded-wait test through a 125-second read-cutoff proxy. This is a local acceptance result; remote GPU/Cloudflare/model acceptance is a separate report.

The acceptance agent did not implement the SDK/runtime and did not use implementation-owner green tests as its conclusion. Old failure evidence is retained alongside current passing results.

## Final evidence

| Check | Independently observed final result | Evidence |
|---|---|---|
| Full seven-phase native HTTP/SDK acceptance | Exit 0, all phases passed; 2026-11-14 22:35:41–22:37:39 +0800 | [JSON](independent_load_results.json), `independent_load.log` (local log) |
| Real 140-second call through 125-second read-cutoff proxy | One long-call submit; six pending responses; stable final ref; cancellation and cleanup pass | [JSON with RPC trace](bounded_wait_results.json), `bounded_wait.log` (local log) |
| Original 100→300→1,000 session churn, then 14 TTLs | Active records 0, bounded diagnostics 64; objects/calls/leases/requests 0, tasks 3 | [JSON](session_churn_results.json) |
| Stale-ID eviction, fresh handshake, inflight-close safety | All checks pass; final active/calls/objects/requests 0, diagnostics 64, tasks 3 | [JSON](session_lifecycle_results.json) |
| Original 128-pending-call pool saturation | Wait pool at 100; heartbeat 0.353 ms, close(wait=False) 13.416 ms; full cleanup | [JSON](connection_pool_results.json) |
| Failed async-with connection entry | All three HTTP sessions closed, sender stopped, client closed | [JSON](failed_open_results.json) |
| Cancelled startup with a real gated HTTP handshake | Pending call fails only after all three pools/sender are cleaned | [JSON](startup_cancel_results.json) |
| Root README start→immediate example→stop | All commands exit 0, no inserted sleep; correct eager/workflow/native shapes | [JSON](readme_quickstart_results.json) |
| Intentionally expired launcher readiness deadline | Own recorded child terminated; port free; ready=false | [JSON](launcher_timeout_results.json) |
| Normal documented full non-GPU pytest command | **81 passed, 2 deselected**, 22.16 s; includes actual no-Torch wheel HTTP client | `full_suite_collection.log` (local log) |
| Original explicit unit→integration collection order | **52 tests collected**, no collection error | `explicit_order_collection.log` (local log) |
| Independent wheel audit | All 15 Python files identical in source, archive and installed package; installed import origin correct; Torch absent | [JSON](wheel_audit_results.json) |
| Full documented Ruff command | All checks passed after the remote owner corrected its script import formatting | `full_ruff.log` (local log) |

## Environment and measurement scope

- WSL Ubuntu-22.04; Python 3.11.11, aiohttp 3.13.2, cloudpickle 3.1.2, torch 2.9.1.
- Real ComfyUI independently verified at `eee59185ff4f43ae2d9ad122b245619d27d9daad`.
- One real CPU native worker, no model weights loaded or downloaded, no custom nodes installed. Small built-in EmptyImage/ImageInvert tensors also execute.
- Every load-harness SDK operation traverses actual loopback HTTP. The harness owns its NativeRuntime/aiohttp server and registers acceptance-only nodes in that process. It does not mock the executor or import the pre-existing test suite.
- SDK clients, server and collector share a process and asyncio loop; native compute uses the runtime's real worker thread. RSS/task counts are **combined harness/server/client measurements**, not isolated daemon memory or GPU metrics. Client GC/JSON work can pause this shared loop.
- Accepted→running includes dependency waiting. Execution is running→completed/failed, not kernel time. Control latency is independent GET `/queue`; SDK heartbeat is recorded separately.

## Measured burst capacity

Each session immediately constructs 128 active calls: eager native→Python→native chains; composed captured graphs containing Python, native lazy selection and expansion; raw API JSON graphs; explicit eager owners of graph results. Each output has four simultaneous awaiters, one cancelled after entering `__await__`, and a sampled subset of Calls has additional pairs of awaiters.

| Sessions | Accepted/completed | Actual awaits started | In-progress waiter cancellations | Duration | Calls/s | Unexpected failures |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 128/128 | 568 | 128 | 1.139 s | 112.42 | 0 |
| 8 | 1,024/1,024 | 4,648 | 1,024 | 8.209 s | 124.75 | 0 |
| 32 | 4,096/4,096 | 18,406 | 4,096 | 55.403 s | 73.93 | 0 |

The three stages executed 5,248 calls, 23,622 actual awaits and 5,248 cancelled waiters. Producers and surviving awaiters completed correctly. Throughput falls and queueing rises at 32 sessions. **32 sessions is a tested capacity point, not a demonstrated maximum or a latency guarantee.**

| Sessions | `/queue` p50/p95/max, ms | Accepted→running p50/p95, ms | Native invocation execution p50/p95, ms | Fetch p95, ms | Combined peak tasks |
|---:|---:|---:|---:|---:|---:|
| 1 | 1.426/4.203/6.424 | 244.202/381.846 | 3.148/15.014 | 3.515 | 635 |
| 8 | 1.235/11.758/379.688 | 2,672.622/4,325.002 | 3.788/12.867 | 15.746 | 6,492 |
| 32 | 2.339/28.765/1,152.112 | 23,729.067/36,503.511 | 7.177/22.632 | 31.913 | 28,475 |

These are burst workloads against serial native execution. They demonstrate correct concurrent admission/await behavior and eventual completion, not parallel native/GPU computation or uniformly low queue latency.

## Large composed workflows

Eight sessions each submit four workflows, plus an eager owner for every result. Each workflow contains 224 declared nodes: two composed 32-step native/Python/expansion chains, 16 unselected failing lazy branches and two output aliases. Every expansion adds two native nodes. Across 32 workflows this exercises 7,168 declared and 4,096 dynamic expansion nodes, in 64 invocations including the owners.

All 32 owned outputs equal `input + 448`; no unselected failing branch executes. All 64 invocations pass in 55.022 s. `/queue` p50/p95/max is 0.963/17.654/**3,932.180 ms**. The observed multi-second tail is retained. The execution distribution includes the tiny owner invocations: p50 550.675 ms and p95 3,135.189 ms.

Combined RSS peaks at **3,131.65 MiB** in this phase; boot RSS is 892.52 MiB and final main-run RSS is 2,135.27 MiB. Correct reference reclamation does not imply an immediate return of process RSS to boot level. No worst-case pause or hard memory ceiling has been established.

## Real long-task / bounded-wait acceptance

A dedicated process starts one actual 140-second native Slow invocation behind an aiohttp reverse proxy configured with a **125-second upstream socket-read timeout**. The final run lasts 140.007 s for that call. Its complete RPC trace proves exactly one submit for the long call and repeated reads of the same call ID/node key; it is never resubmitted.

The SDK performs seven normal 20-second bounded reads: six pending responses and one terminal response. Three additional terminal rereads return the exact same ref, for ten recorded 20-second-parameter queries in total. Maximum query duration is **20.027 s**, below the proxy cutoff. Sixteen local output waiters enter await, four are cancelled, and the remaining twelve receive the same ref and correct fetched value. An extra short bounded read times out to pending, and an abandoned HTTP read does not cancel the producer. Control remains responsive over 278 samples: `/queue` p50 0.966 ms, p95 1.772 ms, max 3.087 ms.

After dropping aliases, while the session is still open, calls/objects/SDK states/watchers all reach zero. After close, active sessions/calls/objects/leases/watchers are zero and baseline tasks are three. Capability-absent compatibility is also independently exercised by hiding only the advertised capability at the proxy: a separate short native call uses exactly one legacy wait query without `wait_seconds`.

This local proxy test proves the bounded-read mechanism under the configured cutoff. It does not replace the separate real Cloudflare/remote report.

## Semantic and API coverage

- Local await does not impose completion dependencies on unrelated calls: an independent call reaches the native queue while a sibling still awaits its inputs.
- Graph capture and symbolic await make no submission; bound intermediates become ready before the whole invocation. Cancelling two of six already-waiting same-output awaiters preserves the other four.
- Ready refs reject await. Multi-slot output tokens are distinct. Releasing one slot while an accepted consumer holds it preserves that consumer and the sibling slot, then reclaims the slot.
- Borrowed workflow outputs raise Expired after native cache eviction. Explicit eager consumers preserve owned results after the original borrowed result expires.
- Native lazy/expand returns correct values; awaiting an unselected output raises Skipped. A failing eager call and its dependent fail while an unrelated call succeeds.
- Cross-session SDK references are rejected and direct HTTP attempts to fetch another session's token fail.
- Real `/prompt`, `/history`, `/queue`, `/ws` routes complete with correct backend UI output. WebSocket listeners disappear after close. Disconnected long-polls with no heartbeat expire and reclaim calls/objects. Following the session repair, the TTL probe first obtains a legitimate server-minted ID; its failure/cleanup assertions are unchanged.
- Eight sessions execute 256 package calls with ordinary absolute/relative imports. Shared process globals produce exactly 1…256; a fresh session after all close returns 257. mtime/mode-only changes preserve hash/globals. Source-file Python executes inside capture. Same-name different content and installed aiohttp conflicts raise PackageConflict.

Across all seven main phases, 5,603 invocations are accepted: 5,599 succeed and four intentionally fail for failure propagation or PackageConflict. No unexpected execution failure occurs.

## Reclamation and fixed defects

Every burst-load session drops all local aliases and triggers local GC **while remaining open**. Each session's remote call/object registry reaches zero within the two-second observation window before close. Every stage ends with calls/objects/leases/listeners/queue zero. Final main-run state is three baseline asyncio tasks, zero active sessions, 62 bounded diagnostic entries and two native cache entries.

The original session churn reproduction formerly retained 1,000 seen records and 1,000 closed records after 14 TTLs, despite no inflight data. It now leaves active records zero and diagnostics 64 at all milestones and after the same wait. The lifecycle probe evicts an old ID from diagnostics and verifies heartbeat/submit/fetch/wait return SessionClosed without recreating it; idempotent close/release also do not recreate it. Handshake proposing an old ID mints another ID; old tokens/locators remain Expired. A closed inflight session is evicted from diagnostics while its accepted input still holds one lease; late native completion safely reclaims captures and leases. **The bounded 64-entry diagnostic deque is not an object leak.**

The original pool reproduction formerly filled one shared 100-connection connector and blocked heartbeat/close beyond 300 ms while separate `/queue` was fast. It now passes at the same saturation point with the native producer still running. Failed client entry and cancelled startup also reclaim all three HTTP sessions and the sender before exposing pending-call failure.

The normal documented suite previously failed the real installed-wheel HTTP client because its SDK was stale, and an explicit selection failed by importing the wrong conftest. Both exact commands now pass, including source/archive/installed byte comparison and a clean no-Torch client that performs eager Python and a graph over actual HTTP. The launcher formerly returned before listen; the same immediate example sequence now passes because start waits for actual `/queue` readiness. A separate 0.001-second readiness-deadline test proves failed startup reclaims its own recorded child.

Original evidence remains in [pool before fix](connection_pool_before_fix.json), [session churn before fix](session_churn_before_fix.json), [failed-open before fix](failed_open_before_fix.json), [quickstart before fix](readme_quickstart_before_fix.json), `full_suite_before_fix.log` (local log), and `explicit_order_collection_before_fix.log` (local log). Earlier observer failures are not product defects: one collector needed the store lock, and the first long-task attempt retained a cancelled Task loop alias. Both harness issues were corrected and their complete workloads independently rerun; the long-task first attempt is retained separately.

## Reproduce and limits

From WSL with the existing provisioned environment:

```sh
cd /home/delva/projects/comani
.venv/bin/python -B -m pytest -q -m 'not gpu' -p no:cacheprovider
.venv/bin/python -B -m pytest tests/unit tests/integration/test_http.py --collect-only -q -p no:cacheprovider
.venv/bin/python -m ruff check src tests scripts
.venv/bin/python -B tests/stress/failed_open_probe.py
.venv/bin/python -B tests/stress/startup_cancel_probe.py
.venv/bin/python -B tests/stress/readme_quickstart_probe.py
.venv/bin/python -B tests/stress/launcher_timeout_probe.py
.venv/bin/python -B tests/stress/audit_wheel.py
.venv/bin/python -B tests/stress/connection_pool_probe.py
.venv/bin/python -B tests/stress/session_churn_probe.py
.venv/bin/python -B tests/stress/session_lifecycle_probe.py
.venv/bin/python -B tests/stress/bounded_wait_probe.py
.venv/bin/python -B tests/stress/independent_load.py
```

Native scripts refuse occupied ports and clean up their own processes. The bounded-wait probe uses 8191/8192; other native probes use 8190. Expected typed native failures print tracebacks; asserted outcomes are recorded in JSON.

This establishes measured CPU small-object behavior through 32 sessions/4,096-call bursts, eight-session 224-declared-node graphs, 1,000-session churn, and one real 140-second bounded-wait call. It does not establish maximum capacities, multi-hour stability, WAN recovery, GPU throughput/VRAM release, model concurrency, broad custom-node compatibility, or sustained raw-history growth. Bootstrap was not repeated during the README check; it used the existing next/.venv. No model was loaded/downloaded, no existing user service was stopped, and no commit/push was made.

Frozen SHA256 independently checked before and after the final tests:

- client.py: `a42515ffb0055993d1597479d6bd1547925952b61962f6d4f7e27643abf77c28`
- runtime service.py: `a9dba0f6424e5c639838f6238dba74daaebceb292cc8d9f8bef3497e9019634f`
- current wheel: `dc2e095acfc4a272042e8499c13c9cc3aef7b701f6ae4d9a48c8333a8b89bcc8`
- main acceptance script: `75cfb646d60804ecb748cc5b7214644513bd67efcaa61f4882a6d3f5f79ba60b`
- bounded-wait script: `2ffc037a91c08b9ae1f0f333131de86887a796c453b920c1e56e1733823247ea`
