# Comani 文档

使用入口见 [README](../README.md)，组件实现以 [设计索引](design/README.md) 为准。

- [远程管理与 Operation](design/remote-management-plan.md)：独立daemon、环境/工具/core/插件准备、bash、生命周期、接纳去重和任务恢复。
- [模型](design/models.md) 与 [文件 I/O](design/files.md)：远端解析、HF/Civitai/HTTP获取、snapshot发布、本地文件流和自动字节续传。
- [SDK/graph](design/sdk.md)、[runtime](design/runtime.md)、[所有权](design/ownership.md)、[协议](design/protocol.md) 和 [Python artifacts](design/python-artifacts.md)。
- [运行与开发](design/development.md)：管理启动、完整CPU示例和单一H3主入口，路径/版本/本地依赖明确选择。
- [管理架构与恢复验收](management_acceptance.md)：本轮已测 wheel、stage 发布、GPU/WAN 与性能证据的精确归属，以及仍待完成的交付门。

本轮管理/恢复/示例的独立结果按上述验收报告中的候选身份核对；后续 node 返回模式按当前 [SDK](design/sdk.md) 与另行验收核对，不借用旧报告认证。旧报告保留其实际成功/失败和构建身份：

| 历史资料 | 范围 |
| --- | --- |
| [性能记录](performance.md) | H3冷热/缓存、refs复用、binary/LZ4、WAN及资源边界 |
| [远端报告](remote_acceptance.md) | 旧cd6/0.2.0官方模板GPU、三路H3、130秒HTTPS与销毁证据 |
| [后台保活报告](../tests/acceptance/HEARTBEAT_REPORT.md) | v4-r2冻结构建、独立跨版本CPU与生命周期 |
| [压力报告](../tests/stress/CURRENT_HEAD_PRESSURE_REPORT.md) | 对应v3会话/churn/阻塞loop/关闭，含基线和失败 |
| [早期压力](../tests/stress/INDEPENDENT_LOAD_REPORT.md) | 原构建CPU/native/GC/代理等待 |
| [薄CLI报告](../tests/acceptance/THIN_CLI_REPORT.md) | 885b8cd的113项CPU验收，不是旧39候选扩展计划 |
| [原始交付](delivery.md)、[环境](environment.json)、[wheel清单](wheel_manifest.json) | 对应时点的历史快照 |

改写后的examples不自动继承历史脚本的GPU数值认证；历史artifacts不会被覆盖成新运行。源码实现、作者回归、独立验收和发布构建是分别核对的事实。
