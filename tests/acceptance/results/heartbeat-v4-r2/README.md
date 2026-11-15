# v4-r2 冻结证据

本目录是 [启动、续约与共享所有权报告](../../HEARTBEAT_REPORT.md) 的精选小证据，
不是新的部署服务或安装器。

| 记录 | 归属与用途 |
|---|---|
| [生产清单](production-manifest.json) | 工程冻结的 17 个 Python 文件及 pyproject hash |
| [工程摘要](engineering-summary.json) | 工程回归、精确变更、公开契约与失败保留路径 |
| [Python 3.12 独立摘要](python312-independent-summary.json) | 实际安装、14 cases、跨 Python source CPU 与四 TTL 阻塞观察 |
| [package 身份](package-identity.json) | 发布方对 source/archive/两个独立安装的静态核对 |
| [增量结构审查](ownership-review.json) | queued/context 根审查的身份与有限范围，非运行验收 |

[压力报告](../../../stress/CURRENT_HEAD_PRESSURE_REPORT.md) 与
[精简原始结果](../../../stress/CURRENT_HEAD_PRESSURE_RESULTS.json) 保留各构建的实际范围。
大日志、原始反例包和未验 GPU source 例程留在本地；本目录不将早期候选的部分绿测当作最终结果。
