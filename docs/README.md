# Comani 文档

当前项目入口是 [README](../README.md)，设计文档和环境记录均位于本项目内。

- [组件设计索引](design/README.md)：SDK、native driver、对象生命周期、协议与 Python artifacts。
- [启动、部署、测试和节点扩展](design/development.md)：已有环境中安装包，再用 `comani daemon` 启动。
- [Python 3.12 与 HTTP 后台保活验收](../tests/acceptance/HEARTBEAT_REPORT.md)：冻结构建、独立跨版本 CPU 与生命周期证据。
- [当前压力报告](../tests/stress/CURRENT_HEAD_PRESSURE_REPORT.md)：v3 的 32 会话、1,000 会话 churn、阻塞 loop 与关闭边界；保留基线和 v2 的失败。
- [历史压力记录](../tests/stress/INDEPENDENT_LOAD_REPORT.md)：原构建的 CPU/native、GC/会话与代理等待测量。
- [就绪与网络入口](design/development.md#就绪与网络入口)：部署者提供环境、模型与网络；Comani 提供 driver。
- [历史远端验收报告](remote_acceptance.md) 与 [当时实际 client 脚本](../examples/minimax_h3_remote.py)：0.2.0 的 HTTPS 基础、130 秒长任务和指定模型三路 2048×2048 PNG 已验证，租用实例已销毁。
- [原始交付测量](delivery.md)、[环境快照](environment.json)、[wheel 清单](wheel_manifest.json)。

测量和验收记录只代表对应环境及范围，不能替代其他硬件、节点或部署的验证。
薄 CLI 的 [113 项 CPU 验收](../tests/acceptance/THIN_CLI_REPORT.md) 属于 885b8cd 基线。当前 v4-r2 的工程 CPU 回归为 184 passed、2 deselected，Python 3.12、后台续约和受影响压力/生命周期已独立复验；新模板 GPU/H3 仍待重测。历史 GPU 结果不改记为当前构建通过，详见 [设计索引](design/README.md#当前边界与证据)。
