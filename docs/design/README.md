# Comfier 组件设计

Comfier 用一个 Python 入口管理远端实例并调用原生 ComfyUI。管理 daemon 独立存活，计算进程持有 ComfyUI、模型与所有远端 Python 对象；SDK 负责参数、传输、引用与本地值。

```text
客户端 Python
  模块 API / Client / Files / Call / Graph / refs
  HF/Civitai/registry 纯查询直接访问上游
                  │ HTTP 管理/文件；可选 ZMQ 计算 gateway
                  ▼
带外 daemon：配置、生命周期、Operation、bash、插件、模型、文件 I/O
                  │ 默认 HTTP 计算；显式可选 IPC 计算；文件/辅助路由 HTTP
                  ▼
带内 ComfyUI 子进程：一个 native worker、执行器、cache、计算 session、对象
```

## 组件地图

| 设计 | 实现 | 职责 |
| --- | --- | --- |
| [SDK 与 graph](sdk.md) | [client.py](../../src/comfier/client.py)、[refs.py](../../src/comfier/refs.py)、[_graph.py](../../src/comfier/_graph.py) | Eager 发送、await、图捕获、metadata、同代计算恢复 |
| [远程管理与 Operation](remote-management-plan.md) | [SDK management](../../src/comfier/management.py)、[管理服务](../../src/comfier/daemon/management/)、[supervisor](../../src/comfier/daemon/supervisor.py) | 环境、版本、工具、插件、bash、生命周期与独立任务 |
| [模型来源与发布](models.md) | [本地 providers](../../src/comfier/_model_providers.py)、[远端 providers](../../src/comfier/daemon/management/providers.py)、[models](../../src/comfier/daemon/management/models.py)、[transports](../../src/comfier/daemon/management/transports.py) | HF/Civitai/HTTP 解析、固定选择、获取、验证和发布 |
| [Runtime](runtime.md) | [service](../../src/comfier/daemon/service.py)、[adapter](../../src/comfier/daemon/adapter.py) | 依赖 admission、原生计算、cache 与观察 |
| [所有权与恢复](ownership.md) | [refs](../../src/comfier/refs.py)、[objects](../../src/comfier/daemon/objects.py) | Owned/borrowed、GC、会话恢复与失效 |
| [协议](protocol.md) | [transport](../../src/comfier/transport.py)、[管理 HTTP](../../src/comfier/daemon/management_http.py)、[计算 HTTP](../../src/comfier/daemon/http.py) | 分层握手、RPC、二进制、去重与续传 |
| [可选 ZMQ 计算通道](compute-transport.md) | [ZMQ SDK](../../src/comfier/_zmq_transport.py)、[ZMQ server](../../src/comfier/daemon/zmq.py) | 同协议 IPC/TCP、显式内部通道、HTTP companion 与 CURVE |
| [Python artifacts](python-artifacts.md) | [code](../../src/comfier/code.py)、[python_node](../../src/comfier/daemon/python_node.py) | callable/source/package 传输与正常 imports |
| [文件 I/O](files.md) | [客户端 files](../../src/comfier/files.py)、[FileStore](../../src/comfier/daemon/files.py)、[file_http](../../src/comfier/daemon/file_http.py) | 原生 roots、显式流、自动字节续传与持久文件 |
| [运行与开发](development.md) | [CLI](../../src/comfier/cli/)、[pyproject](../../pyproject.toml) | 必要启动入口、示例、开发和冻结验证 |

管理实现位于 `daemon/` 与 `cli/`；公开命令统一进入 `cli/`，再调用 `daemon/`；daemon 不依赖 CLI。

## 核心约束

1. node/Python/submit 调用即进入发送流程；await 只约束当前协程，不是整个设备的全局屏障。
2. graph 只收集明确的计算调用；bash/files/管理方法仍是实际 I/O，重放 graph 不重跑它们。
3. provider/registry 纯查询在客户端执行、无需 Client；目标配置、工具/插件/模型文件状态和 roots 必须 RPC 到 daemon。
4. Tensor/模型与计算 session 留在计算子进程；daemon 的代理不解码 Tensor。
5. 连接不安装、不启动；环境变更和生命周期由脚本显式执行，失败自然中断顺序流程。
6. 网络恢复观察原调用/操作，不重跑脚本、副作用或已失效的 refs。

## 范围与验证

Comfier 提供单实例的管理、计算、模型与文件接口。必要 CLI 用于启动管理 daemon，实例内的准备与操作通过管理 API 完成。多实例调度、通用 DSL、永久任务数据库和跨 daemon 崩溃 exactly-once 不属于当前范围。

测试构建、环境、GPU/WAN 与性能结果见[验收与运行记录](../README.md#验收与运行记录)。具体结论以各报告记录的构建与实际运行范围为准。
