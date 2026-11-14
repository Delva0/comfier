# Thin CLI 0.3.0 CPU acceptance evidence

These selected records support the [independent report](../../THIN_CLI_REPORT.md).
They describe the tested `0.3.0` wheel and reviewed script cleanup. The cloud GPU
results at commit `758738e` remain historical evidence; this build has not yet
repeated that deployment or model run.

| Evidence | Record |
| --- | --- |
| Scope, final test count and exact wheel SHA256 | [Summary](summary.json) |
| Frozen `pyproject.toml` and all 17 package files | [Production manifest](production-manifest.json) |
| Source, wheel archive and installed runtime comparison | [Wheel verification](cleanup-wheel.json) |
| Three independently installed package copies | [Installed identities](installed.json) |
| CLI, cache, import-time device, path and API registry observations | [Acceptance observations](results.json) |
| Ten actual native CPU cases | [CPU acceptance](cpu-acceptance.json) |
| Torch-free HTTP client and node/workflow execution | [HTTP smoke](http-smoke.json) |
| Positional node inputs and execution metadata with dummy credentials | [Metadata](metadata.json) |
| Unsupported options rejected before Torch imports | [Rejections](rejections.json) |
| Sequential default runtimes after native allocator detection | [Repeated runtime](repeat-runtime.json) |
| Reviewed cleanup, migrated tool help and identity checks | [Cleanup](cleanup-results.json) |
| CLI startup, basic example and owned process shutdown | [Quickstart](cleanup-quickstart.json) |
| Startup deadline and process cleanup | [Timeout](cleanup-timeout.json) |
| Occupied port, early exit and caller-selected evidence directory | [Lifecycle](cleanup-lifecycle.json) |
| Short wait with one submit and one execution | [Short wait](migrated-short-wait.json) |

Raw logs, test probes, the first allocator-guard failure and wheel files remain
local in `.local/thin-cli-acceptance/`. They are not published download links.
The short wait record is a 0.05-second CPU probe, not a new long-wait measurement.
