# Comani 设计文档

本文档对应 [本仓库](../../) 的当前实现。ComfyUI 是执行设备，daemon 是该设备的下发入口；本地 SDK 负责描述调用和处理引用，不接管原生模型加载、显存管理或图控制语义。

```text
本地 Python
  Client / Call / OutputFuture / RemoteRef
                 │ JSON RPC + code artifacts
                 ▼
daemon 主事件循环：接收、依赖等待、会话与对象记录
                 │ 就绪调用
                 ▼
一个 native worker → 原生 ComfyUI 执行器与缓存
```

## 组件地图

| 设计 | 实现 | 职责 |
|---|---|---|
| [SDK 与 graph](sdk.md) | [client.py](../../src/comani/client.py)、[refs.py](../../src/comani/refs.py)、[graph.py](../../src/comani/graph.py) | Eager 提交、局部等待、符号捕获与 run 绑定 |
| [Runtime](runtime.md) | [service.py](../../src/comani/_runtime/service.py)、[adapter.py](../../src/comani/_runtime/adapter.py) | 每调用依赖、就绪队列、原生执行与结果观察 |
| [所有权](ownership.md) | [refs.py](../../src/comani/refs.py)、[objects.py](../../src/comani/_runtime/objects.py) | Owned/borrowed、GC、ACK 接管与失效 |
| [协议](protocol.md) | [protocol.py](../../src/comani/protocol.py)、[transport.py](../../src/comani/transport.py)、[http.py](../../src/comani/_runtime/http.py) | RPC、值编码、错误与辅助事件流 |
| [Python artifacts](python-artifacts.md) | [code.py](../../src/comani/code.py)、[python_node.py](../../src/comani/_runtime/python_node.py) | 代码打包、通用 Python 节点、正常包 imports |
| [开发与扩展](development.md) | [scripts/](../../scripts/)、[pyproject.toml](../../pyproject.toml) | 环境、启动、原生节点接入与测试 |

## 设计约束

1. `.node/.python/.submit` 返回的 Call 已进入活动发送流程；await 不是启动开关。
2. 本地 await 约束当前协程的后续执行，不是 daemon 全局屏障。
3. 数据依赖来自显式传入的引用；独立调用没有人为添加的完成顺序边。
4. Graph 是显式的原生工作流描述，构图时不执行计算，不捕获任意 Python 控制流。
5. Lazy、loop、动态展开、list mapping 等交给原生执行器。
6. 读取引用不触发重新执行；失效、跳过与失败必须明确结束等待。

## 当前边界与证据

SDK 不导入 Torch/ComfyUI，不生成 `.nodes` 命名空间或节点源码。一个 daemon 对应一个原生 runtime；跨实例对象迁移、自动负载均衡、前端 canvas 与完整 ComfyUI HTTP 兼容层均未实现。

原始环境和交付记录在 [delivery.md](../delivery.md)，其内容是对应时点的测量快照。新增压力与远端验收由独立 agent 记录，最终结果应从独立报告读取，不能由实现者自检推导。

| 独立验收 | 当前入口与状态 |
|---|---|
| 本地 CPU/native、持续提交、GC 与 session churn | [独立压力报告](../../tests/stress/INDEPENDENT_LOAD_REPORT.md)：通过；标准非 GPU suite 81 passed、2 deselected，32 sessions/4,096 calls 和 224 节点组合图通过 |
| 长调用经过读取超时代理 | 同一[独立报告](../../tests/stress/INDEPENDENT_LOAD_REPORT.md)：本地 140 秒调用经过 125 秒读取超时代理通过，调用仅提交一次；不是实际远端 HTTPS 验证 |
| 真正远端 HTTPS 基础执行 | [10 项 smoke](../../artifacts/remote/60662518-20261114/smoke/acceptance.json)、[130 秒单次提交](../../artifacts/remote/60662518-20261114/long-wait.json) 与小型 NVFP4 诊断通过；部署方式见 [开发指南](development.md#远端入口) |
| 指定 GPU 模型、图片与资源清理 | 仍待完整验收，[远端报告](../remote_acceptance.md) 记录进度；实际 comani 脚本为 [minimax_h3_remote.py](../../examples/minimax_h3_remote.py) |

32 sessions 是实测容量点，不是最大容量或延迟保证。CPU 压力测试不加载模型，模型资源需求须由对应远端实测确定；报价中的 RAM 数字不能当作实际 cgroup 限额或最低配置。
