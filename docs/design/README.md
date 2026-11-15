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
| [启动与开发](development.md) | [CLI](../../src/comani/__main__.py)、[runtime 入口](../../src/comani/_runtime/__main__.py)、[pyproject.toml](../../pyproject.toml) | 当前解释器内启动 driver、节点接入与开发测试 |

## 设计约束

1. `.node/.python/.submit` 返回的 Call 已进入活动发送流程；await 不是启动开关。
2. 本地 await 约束当前协程的后续执行，不是 daemon 全局屏障。
3. 数据依赖来自显式传入的引用；独立调用没有人为添加的完成顺序边。
4. Graph 是显式的原生工作流描述，构图时不执行计算，不捕获任意 Python 控制流。
5. Lazy、loop、动态展开、list mapping 等交给原生执行器。
6. 读取引用不触发重新执行；失效、跳过与失败必须明确结束等待。

## 当前边界与证据

当前 Git checkpoint 保留版本号 `0.3.0`，Python 3.12 启动、HTTP 独立保活与共享所有权已按明确构建身份验收。native/headless 支持范围见 [Runtime](runtime.md)，metadata 用法见 [SDK](sdk.md#可选执行-metadata)。

| 范围 | 构建与证据 |
|---|---|
| 当前 v4-r2 / `64a73a6e…` wheel | [启动与保活验收](../../tests/acceptance/HEARTBEAT_REPORT.md)：工程 CPU 184 passed、2 deselected；独立 Python 3.12 定向回归和跨版本 source 三路 CPU 链通过。 |
| HTTP 独立保活与回收 | [独立压力报告](../../tests/stress/CURRENT_HEAD_PRESSURE_REPORT.md)：32 会话/4,096 calls、1,000 会话短 TTL churn、业务 loop 阻塞超过 TTL 后 ownedRef 可用，GC 在 close 前完成；无活跃会话意外失效。关闭中的空会话允许 TTL 兜底回收，不承诺所有历史运行零 expiry。 |
| 885b8cd、被否决 v2/v3 与旧 v4 | 保留业务 loop 心跳失活、暂态错误/HTTP 503 误拦及 queued/context 隐藏拥有者的反例；早期候选的部分通过不改记为最终通过。 |
| 新版官方模板 GPU/H3 旅程 | 仍待使用固定公开 commit 独立重测；历史 758738e 图片不替代此项验收。 |

专用 keeper 只负责 HTTP lease 续约；业务 RPC、GC 控制消息仍需要业务 loop。进程/OS 冻结、任意长 GIL 占用和网络中断不在保活保证内，详见 [会话语义](ownership.md#会话续约与业务-loop-暂停)。

下面保留历史执行证据。[delivery.md](../delivery.md) 中的环境和旧 GPU 结果是对应时点的测量；历史压力记录的源文件 hash 与当前发布安装不完全相同，不能直接转作最终合并版的逐字节认证。

| 历史验收 | 保留记录 |
|---|---|
| 本地 CPU/native、持续提交、GC 与 session churn | [独立压力报告](../../tests/stress/INDEPENDENT_LOAD_REPORT.md)：通过；标准非 GPU suite 81 passed、2 deselected，32 sessions/4,096 calls 和 224 节点组合图通过 |
| 长调用经过读取超时代理 | 同一[独立报告](../../tests/stress/INDEPENDENT_LOAD_REPORT.md)：本地 140 秒调用经过 125 秒读取超时代理通过，调用仅提交一次；不是实际远端 HTTPS 验证 |
| 真正远端 HTTPS 基础执行 | [10 项 smoke](../../artifacts/remote/60662518-20261114/smoke/acceptance.json)、[130 秒单次提交](../../artifacts/remote/60662518-20261114/long-wait.json) 与小型 NVFP4 诊断通过；当前入口见 [启动指南](development.md) |
| 指定 GPU 模型、图片与资源清理 | 指定 INT8 模型三路 2048×2048 出图逐位一致，租用实例已销毁；[远端报告](../remote_acceptance.md)、[实际脚本](../../examples/minimax_h3_remote.py) 与 [样图](../../artifacts/remote/60662518-20261114/eager.png) |

32 sessions 是实测容量点，不是最大容量或延迟保证。CPU 压力测试不加载模型，模型资源需求须由对应远端实测确定；报价中的 RAM 数字不能当作实际 cgroup 限额或最低配置。
