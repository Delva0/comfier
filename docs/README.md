# Comfier 文档

[项目 README](../README.md)介绍安装、启动与基本调用；[运行指南](design/development.md)提供环境准备、模型目录配置和开发命令。

## 使用与设计

- [SDK 与 graph](design/sdk.md)：直接输出、Call、图输入/导出、节点观察与原生 API prompt。
- [远程管理与 Operation](design/remote-management-plan.md)：环境、工具、ComfyUI、插件、bash、生命周期与受管任务。
- [模型](design/models.md)与[文件 I/O](design/files.md)：provider 查询、远端下载、模型发布、文件传输与续传。
- [Python 代码](design/python-artifacts.md)：callable、源码文件与包传输。
- [Runtime](design/runtime.md)、[所有权](design/ownership.md)与[协议](design/protocol.md)：计算、引用、会话与传输契约。
- [ZMQ 计算通道](design/compute-transport.md)与[原生 HTTP 控制](design/native-controls.md)：可选计算传输及后端 JSON 路由。
- [组件设计索引](design/README.md)：组件职责与实现位置。
- [示例目录](../examples/README.md)与[改稿交付](editable-workflows.md)：按任务选择工作流，检查可编辑产物。

## 验收与运行记录

各报告记录其实际测试的构建、环境、结果与限制。

| 资料 | 范围 |
| --- | --- |
| [公共 API 验收](public_api_acceptance.md) | CPU 回归、真实示例、并发与生命周期 |
| [管理架构与恢复验收](management_acceptance.md) | 管理、恢复、模型发布及 GPU/WAN 证据 |
| [工作流复现研究](research/workflow-reproduction-20261116.md) | 附件准备、Score 调优与工作流运行记录 |
| [性能记录](performance.md)与[管理性能](management_performance.md) | H3、refs、传输、下载与资源测量 |
| [远端报告](remote_acceptance.md) | 官方模板 GPU、H3、HTTPS 与实例销毁记录 |
| [后台保活](../tests/acceptance/HEARTBEAT_REPORT.md)、[压力测试](../tests/stress/CURRENT_HEAD_PRESSURE_REPORT.md)、[早期压力](../tests/stress/INDEPENDENT_LOAD_REPORT.md) | 对应构建的会话、等待、恢复与资源结果 |
| [CLI 验收](../tests/acceptance/THIN_CLI_REPORT.md) | CLI 入口与 CPU 验收 |
| [交付快照](delivery.md)、[环境](environment.json)、[wheel 清单](wheel_manifest.json) | 对应时点的交付身份 |
