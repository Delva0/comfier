# Comani 文档

当前项目入口是 [README](../README.md)，设计文档和环境记录均位于本项目内。

- [组件设计索引](design/README.md)：SDK、native driver、对象生命周期、协议与 Python artifacts。
- [开发、部署、测试和节点扩展](design/development.md)。
- [本地独立验收](../tests/stress/INDEPENDENT_LOAD_REPORT.md)：CPU/native、负载、GC/会话与代理等待。
- [远端部署指引](design/development.md#远端入口)：环境、wheel、daemon 就绪和模型准备分开。
- [远端验收进度](remote_acceptance.md) 与 [实际 comani client 脚本](../examples/minimax_h3_remote.py)：HTTPS 基础与长任务已验证，完整模型例程和 PNG 仍待验收。
- [原始交付测量](delivery.md)、[环境快照](environment.json)、[wheel 清单](wheel_manifest.json)。

测量和验收记录只代表对应环境及范围，不能替代其他硬件、节点或部署的验证。
