# Comani 文档

当前项目入口是 [README](../README.md)，设计文档和环境记录均位于本项目内。

- [组件设计索引](design/README.md)：SDK、native driver、对象生命周期、协议与 Python artifacts。
- [启动、部署、测试和节点扩展](design/development.md)：已有环境中安装包，再用 `comani daemon` 启动。
- [本地独立验收](../tests/stress/INDEPENDENT_LOAD_REPORT.md)：CPU/native、负载、GC/会话与代理等待。
- [就绪与网络入口](design/development.md#就绪与网络入口)：部署者提供环境、模型与网络；Comani 提供 driver。
- [远端验收报告](remote_acceptance.md) 与 [实际 comani client 脚本](../examples/minimax_h3_remote.py)：HTTPS 基础、130 秒长任务和指定模型三路 2048×2048 PNG 已验证，租用实例已销毁。
- [原始交付测量](delivery.md)、[环境快照](environment.json)、[wheel 清单](wheel_manifest.json)。

测量和验收记录只代表对应环境及范围，不能替代其他硬件、节点或部署的验证。
历史 758738e 记录属于原执行基线。本轮薄 CLI、原生选项和 execution metadata 已完成 [独立 CPU 验证](../tests/acceptance/THIN_CLI_REPORT.md)，标准非 GPU suite 为 113 passed、2 deselected；新的 ComfyUI 模板 GPU 重测尚待后续。
