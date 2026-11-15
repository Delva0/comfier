# 远程实例管理与 Operation

本文描述当前实现。带外 daemon 管理一个 ComfyUI 计算子进程，在未安装、停止或启动失败时继续提供管理能力。SDK 通过同一个 Client 使用管理、文件与计算；连接只做管理握手，首次 node/Python/submit 才惰性附着已经显式启动的计算进程。

实现：[SDK namespace/DTO](../../src/comani/management.py)、[supervisor](../../src/comani/daemon/supervisor.py)、[管理服务](../../src/comani/daemon/management/service.py)、[Operation](../../src/comani/daemon/management/operations.py)、[管理 HTTP](../../src/comani/daemon/management_http.py)。

## 进程、解释器与维护

| 所在位置 | 职责 |
| --- | --- |
| 客户端 | 普通参数、DTO、传输、观察、必要的本地文件读写 |
| 管理 daemon | 配置、PID、生命周期、bash、插件/模型后端、文件存储、Operation 和有界日志 |
| ComfyUI 子进程 | 原生执行器/cache、模型/Tensor/Python 对象、graph、计算 session/Call/refs |

一个公开端口将计算 RPC、二进制/LZ4 fetch、辅助 HTTP 和 WebSocket 按有界原始字节流代理到 loopback 计算端口。管理进程不反序列化 Tensor。管理与计算拥有独立 epoch；停止计算不停止管理和文件传输。

配置明确记录 `comfy_root/runtime_python/workspace/cpu/custom_nodes/comfy_args`。环境、core 和插件变更必须先显式停止计算，并由实例维护锁协调；运行中变更明确报 RuntimeBusy。stop/restart 默认 `wait=True`，等待已接纳计算、实际 native 和本实例受管环境安装完成，再停止/重启。显式 `wait=False` 是紧急模式：取消本实例正在执行及排队的专用环境/插件维护、待启动控制，停止其所属子进程组并释放锁，再中断计算并停止或尝试重启。普通 bash、files 和模型下载继续。被中断维护保留 cancelled/partial 事实，不承诺 pip 或脚本回滚；半安装状态可能使 restart 失败，带外 daemon 仍可查询、运行 bash 和修复。`force=True` 仅兼容显式紧急模式，`wait=True, force=True` 拒绝。生命周期控制不占计算队列或普通管理 worker 槽位。

wait 参数决定是否先等待相关工作，并不改变 Python await：`await remote.comfyui.restart(wait=False)` 仍等待重启成功或失败。需要先拿观察句柄时使用 `begin_restart()`；结果中的 `interrupted_operations` 可查询被紧急控制中断的原 Operation。紧急控制期间拒绝新的相关维护接纳，同 key 仍可附着原操作，已排队维护不会随后悄悄继续改环境或启动 runtime。

管理 Python 不需要 Torch。目标依赖只装入明确选定的解释器。启动时 daemon 将自身当前 comani 包复制到隔离 bridge 目录，只暴露该包，不把整个管理 site-packages 或继承的 PYTHONPATH 注入目标。目标 aiohttp/cloudpickle、Torch/节点依赖仍从目标解释器解析。启动检查依赖，绝不自动 pip 或替换 Torch；`runtime_bridge_digest` 记录实际桥接代码身份。

## 显式准备环境

```python
env = await remote.comfyui.create_environment("/srv/comfy-env")
await remote.comfyui.configure(
    runtime_python=env.python, comfy_root="/srv/ComfyUI",
    cpu=True, custom_nodes=False,
)
await remote.comfyui.prepare_tools(
    cli_revision="4f554bb5a9e5d0b5d04146e7bdf8cac2ceb729a5",
    manager_version="4.2.2",
)
await remote.comfyui.install(
    commit="eee59185ff4f43ae2d9ad122b245619d27d9daad",
    skip_torch=False,
)
await remote.comfyui.prepare_runtime("/srv/packages/current-comani.whl")
await remote.comfyui.start()
```

路径均在服务器。`create_environment(path, python=None)` 创建不使用 system site-packages 的独立 venv；已有目录拒绝，不默修旧环境。默认 base Python 是管理解释器，可明确指定服务器上的另一个 Python。已有模板可直接 configure 其解释器，不必创建 venv。

`prepare_tools` 是明确安装请求，固定 comfy-cli commit 与 packaged Manager 4.2.2；`hub_version` 可显式安装所选精确 huggingface-hub 版本。它读回实际包版本/来源并执行 pip check，不在缺包时暗装工具。`prepare_runtime(package)` 安装调用方选定的 wheel/requirement 并读回目标环境依赖，建议使用本次构建的精确 wheel。

CPU 安装需 `configure(cpu=True)` 和 `install(skip_torch=False)`。GPU 环境的 Torch/CUDA build 由调用方选择，可用 `bash.run` 对明确的 `runtime_python -m pip` 安装完整固定 requirement 与 index，再 `install(skip_torch=True)`；不从硬件猜测并安装 CUDA。pip check 只证明包依赖关系，不证明 CUDA/ABI 或模型健康。

`install/update` 可选择 `commit=完整40位SHA` 或 `version=...`，不能同时给两者。首次接纳将 tag/latest/nightly 固定到实际 commit；未指定 install 目标使用上面的固定 ComfyUI commit。实际 HEAD 必须匹配，dirty 工作树拒绝。更新已有源码使用 update；配置和启动不会重置已有目录。

## 静态 namespace

所有查询都到远端获取实际事实，包括 info/config、backend/list/versions、resolve/manifest 和 roots；不在客户端查询 pip/Git、解析上游或缓存一个旧状态当作新结果。DTO 是普通本地值，不拥有远端对象。

| namespace | 查询 | 明确变更 |
| --- | --- | --- |
| comfyui | info/config/logs | configure/create_environment/prepare_tools/prepare_runtime/install/update/start/stop/restart |
| plugins | backend/list/versions(package) | install/update/enable/disable/uninstall；install_git/update_git |
| models | categories/list/versions/resolve/manifest | download/snapshot/remove |
| bash | 无独立本地查询 | run/begin |

大部分变更都有 `begin_动作(...)` 返回 ManagementOperation；短写直接等待 result。具体参数见源码静态方法；模型选择与 snapshot 见 [模型设计](models.md)，本地文件见 [文件 I/O](files.md)。

失败和部分失败抛带实际逐项结果的 OperationError，顺序脚本自然停止。不能因为底层进程 exit 0 就声称安装成功：core HEAD、插件实际目录/版本/启用状态、目标 Python 和依赖步骤都会读回。结果会区分磁盘变更、依赖步骤、restart_required 与 partial_changes；安装后需显式启动/重启才更新 live registry。取消不承诺回滚。

## 插件后端

```python
backend = await remote.plugins.backend()
available = await remote.plugins.versions("requested-package")
changes = await remote.plugins.install(["requested-package"], version="1.2.3")
await remote.plugins.disable(["requested-package"])
await remote.plugins.enable(["requested-package"])
# Git-only packages use an explicit separate entry point:
await remote.plugins.install_git(
    "https://example.org/owner/nodes.git", commit=SELECTED_FULL_COMMIT,
)
```

示例中的包 ID、版本和 Git URL 是调用方的选择，不能直接作为存在证明。Registry 安装先远端解析固定版本，再交给 Manager；nightly 对应 Git 来源。Git install/update 将未给定的 HEAD 在 daemon 固定一次，拒绝 dirty，读回实际 commit。更新只接指定包，拒绝 all/comfy/comfyui，避免扩展成隐式 core 更新。

工具经目标解释器的独立子进程运行，不 import 有全局 cwd/env/单例的 CLI 模块到 daemon。实例专属工具 HOME 和显式 workspace 避免借用其他安装；解释器仍要读回核对。packaged Manager 4.2.2 的 enable/disable 采用其原生 .disabled 目录规则、create-only 原子移动和独立读回。`no_deps=True` 不保证安装脚本不运行 install.py。

## Bash

```python
result = await remote.bash.run(
    "printf '%s\\n' \"$MESSAGE\"",
    cwd="/srv", env={"MESSAGE": "hello"}, timeout=30, check=True,
)
print(result.exit_code, result.stdout, result.stderr)
```

每次是独立非交互 `bash --noprofile --norc -c`，Linux/bash，无 PTY/stdin。cwd 默认实例 workspace，cd/export 不跨调用持久化；env 仅覆盖该进程，不自动注入管理/provider 凭证。shell 中 python 由 PATH 决定，进入 ComfyUI 目录不等于切换解释器。

原始 bash 是独立 OS 通道，不要求 runtime 停机，也不持有环境维护锁；长命令不阻挡 stop/restart。daemon 不分析脚本文本来推断是否修改环境。调用方需要改变运行环境时，显式安排停止计算和专用安装/更新 API；普通 shell 命令自行承担其 OS 副作用。

默认 `check=True`，非零退出抛 CommandError，details 含 CommandResult；`check=False` 返回退出事实。timeout 与显式 cancel 另报操作错误，并按进程组 TERM→KILL→reap 收尾。stdout/stderr 并发读取、分别标明截断，不保证跨流全序。一个脚本的前序步骤不会因后序失败回滚。

bash/files/管理方法都是普通 async I/O，在 await/调度时执行；不接收 RemoteRef/Future，也不产生 Call/输出槽。在 graph scope 内执行的 bash 仍立即发生，提交 graph 不再重复它。

## Operation 执行与观察

```python
operation = await remote.bash.begin("sleep 2; printf done", check=True)
handle = operation.export_handle()
status = await operation.status()
async for entry in operation.logs():
    print(entry.stream, entry.text, end="")
result = await operation.result()
# Advanced: a fresh Client can observe the same accepted operation.
recovered = await another_remote.operations.get(handle=handle)
```

常规 await 内部使用相同机制，无需用户维护 ticket、key 或 reconnect loop。Operation 支持 status/result、log_page 和 logs(after=cursor, follow=True)、cancel、export_handle；没有 fetch、GraphRef 或计算 cache。log cursor 单调，过期返回 LogExpired 及有效起点，不静默跳页。

| 事件 | 管理任务 | 计算 |
| --- | --- | --- |
| 网络中断/本地等待取消 | 接纳任务继续；恢复观察原 ID | 同代宽限内恢复原 session/call |
| Client.close | 停止本地观察，已接纳任务继续 | 明确撤销计算 session，离线关闭未确认会报告 |
| Operation.cancel / 自身 timeout | 清理所属进程/网络，保留完成或 partial 事实 | 不等于计算 HTTP 等待 timeout |
| 计算 stop/restart | daemon 与管理操作继续 | 新 epoch，旧 refs Expired |
| 管理 daemon 重启 | 旧句柄报换代/未知，不创建替代任务 | 不保证跨管理进程崩溃恢复 |

SDK 首次发送前固定原 daemon epoch、服务器签发的 submission window、key 和规范化参数；daemon 在外部动作前登记 key/digest/Operation。相同 key/digest 返回原 ID，不同参数冲突。默认 300 秒 window 限制未知操作的首次接纳，过期的未知请求不执行；已知 key 在结果保留期内仍返回原操作。lost reply 不生成新 key 补跑。

默认资源边界：64 个 active、8 个 running、4096 条 retained receipts，终态结果保留一小时。每结果最多 8 MiB，全局 serialized results 128 MiB，接纳先预留结果额度，结束后按实际结算。超大结果返回有界 OperationResultTooLarge 与 completed_state/partial 事实，不重跑操作。全局日志 32 MiB、每操作 256 KiB；单进程 stdout/stderr 各最多 1 MiB，整个操作 capture 最多 2 MiB，并明确截断。容量满拒绝新接纳，已接纳任务仍可查，不逐出仍受窗口保护的 receipt。

可通过 `create_management_app(supervisor, management_limits={...})` 配置管理限额，或先构造 `ManagementService(supervisor, operation_limits={...})` 再以 service= 注入；两者不能同时给。握手/window 公布有效值。它们分别控制执行、receipt、结果和日志，不是无限历史数据库。计算/文件 policy 是 [Runtime](runtime.md) 与 [文件 I/O](files.md) 的独立边界。

## 验证与范围

作者已完成空白轻量 management 环境经 API 创建隔离 CPU 环境、安装 core/工具/bridge、启动/停止，以及固定 Manager registry 安装/禁用/启用/卸载的实际读回。作者回归不是新的独立 CPU/GPU/WAN 验收；当前最终构建、故障恢复、远端模型和硬件结果应由新报告逐项记录。

不承诺跨 daemon crash exactly-once、历史数据库、通用作业调度、自动租机、自动 apt/环境修复或旧 39 项 CLI 扩展。只恢复原任务的观察，不重新执行整段 Python 脚本。
