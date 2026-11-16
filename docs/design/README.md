# Comani 组件设计

Comani 用一个 Python 入口管理远端实例并调用原生 ComfyUI。管理 daemon 独立存活，计算进程持有 ComfyUI、模型与所有远端 Python 对象；SDK 负责参数、传输、引用与本地值。

```text
客户端 Python
  Client / 管理 namespace / Files / Call / Graph / refs
                  │ 一个公开 HTTP endpoint
                  ▼
带外 daemon：配置、生命周期、Operation、bash、插件、模型、文件 I/O
                  │ bounded 原始 HTTP/二进制/WS 字节流代理
                  ▼
带内 ComfyUI 子进程：一个 native worker、执行器、cache、计算 session、对象
```

## 组件地图

| 设计 | 实现 | 职责 |
| --- | --- | --- |
| [SDK 与 graph](sdk.md) | [client.py](../../src/comani/client.py)、[refs.py](../../src/comani/refs.py)、[graph.py](../../src/comani/graph.py) | Eager 发送、await、图捕获、metadata、同代计算恢复 |
| [远程管理与 Operation](remote-management-plan.md) | [SDK management](../../src/comani/management.py)、[管理服务](../../src/comani/daemon/management/)、[supervisor](../../src/comani/daemon/supervisor.py) | 环境、版本、工具、插件、bash、生命周期与独立任务 |
| [模型来源与发布](models.md) | [providers](../../src/comani/daemon/management/providers.py)、[models](../../src/comani/daemon/management/models.py)、[transports](../../src/comani/daemon/management/transports.py) | HF/Civitai/HTTP 解析、固定选择、获取、验证和发布 |
| [Runtime](runtime.md) | [service](../../src/comani/daemon/service.py)、[adapter](../../src/comani/daemon/adapter.py) | 依赖 admission、原生计算、cache 与观察 |
| [所有权与恢复](ownership.md) | [refs](../../src/comani/refs.py)、[objects](../../src/comani/daemon/objects.py) | Owned/borrowed、GC、会话恢复与失效 |
| [协议](protocol.md) | [transport](../../src/comani/transport.py)、[管理 HTTP](../../src/comani/daemon/management_http.py)、[计算 HTTP](../../src/comani/daemon/http.py) | 分层握手、RPC、二进制、去重与续传 |
| [Python artifacts](python-artifacts.md) | [code](../../src/comani/code.py)、[python_node](../../src/comani/daemon/python_node.py) | callable/source/package 传输与正常 imports |
| [文件 I/O](files.md) | [客户端 files](../../src/comani/files.py)、[FileStore](../../src/comani/daemon/files.py)、[file_http](../../src/comani/daemon/file_http.py) | 原生 roots、显式流、自动字节续传与持久文件 |
| [运行与开发](development.md) | [CLI](../../src/comani/cli/)、[pyproject](../../pyproject.toml) | 必要启动入口、示例、开发和冻结验证 |

文件名 `remote-management-plan.md` 保留旧链接，其内容现为当前实现设计，不再是未来 API 提案。实现位于 `daemon/` 与 `cli/`；公开命令统一进入 `cli/`，再调用 `daemon/`；daemon 不依赖 CLI。

## 核心约束

1. node/Python/submit 调用即进入发送流程；await 只约束当前协程，不是整个设备的全局屏障。
2. graph 只收集明确的计算调用；bash/files/管理方法仍是实际 I/O，重放 graph 不重跑它们。
3. 所有管理查询都 RPC 到 daemon；SDK 不在本机解析来源、查询目标版本或伪造 roots。
4. Tensor/模型与计算 session 留在计算子进程；daemon 的代理不解码 Tensor。
5. 连接不安装、不启动；环境变更和生命周期由脚本显式执行，失败自然中断顺序流程。
6. 网络恢复观察原调用/操作，不重跑脚本、副作用或已失效的 refs。

## 当前边界与证据

管理、恢复、模型与文件续传已在源码中实现；源码实现、作者回归、独立验收和最终 wheel 身份是不同事实。[本轮管理验收](../management_acceptance.md) 按已测候选分别记录独立 CPU、stage 发布、GPU/WAN 和性能证据，不把其后的未定 API 工作区改动归入已测 wheel。

历史资料保持原有结果：`64a73a6e…` 的 [启动与保活报告](../../tests/acceptance/HEARTBEAT_REPORT.md)、[压力报告](../../tests/stress/CURRENT_HEAD_PRESSURE_REPORT.md)、[GPU/H3 远端报告](../remote_acceptance.md) 及 [files/binary/LZ4 性能记录](../performance.md)。原来的失败与资源限制仍按各报告解读；本轮实际执行的冻结示例、图片和新性能结果另列于管理验收，不能替换历史归属。

本轮不扩充旧的 39 项 CLI 候选，不建立通用调度器/DSL、历史任务数据库、跨 daemon 崩溃 exactly-once 或隐式环境修复。必要 CLI 仅用于启动管理 daemon 与计算桥接；使用管理 API 完成实例内的工作。
