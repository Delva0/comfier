# SDK 调用与 graph 语义

实现：[client.py](../../src/comani/client.py)、[refs.py](../../src/comani/refs.py)、[graph.py](../../src/comani/graph.py)。

## 管理连接与计算附着

`async with Client(url, token=...)` 只做管理握手，ComfyUI 未安装/停止时也可查询、准备环境和使用 bash/files。显式 `await remote.comfyui.start()` 后首个计算调用才惰性附着计算 session。连接从不自动安装或启动。旧 direct native/in-process transport 保持其原有计算入口语义。

`remote.comfyui/plugins/models/bash` 是静态 namespace，其查询和变更都是真实远端 I/O；返回 DTO/metadata 是本地值。与 files 一样，普通 async 方法在 await/调度时执行，即使位于 graph scope 内也不被捕获。准备、管理 Operation 和恢复见 [管理设计](remote-management-plan.md)。

`comfyui.stop()` / `restart()` 默认 `wait=True`，等待计算和相关受管安装完成。显式 `wait=False` 中断计算及本实例受管维护后停止或尝试重启；不保证安装回滚，结果保留被中断 Operation，重启失败也明确报告。`wait=True` 与 `force=True` 不能同时使用。Python `await restart(wait=False)` 仍等待重启结果；`begin_restart` 才先返回观察句柄。实际计算代际结束后旧调用与引用失效。bash 可在 runtime 运行时执行，长 shell 不阻挡生命周期控制。

## Eager 调用

```python
async with Client(url, token=token) as remote:
    a = remote.node("OriginalClassName", **{"input name": value})
    b = remote.node("Consumer", value=a)
    result = await b
```

节点名称直接使用 registry 的原始字符串；常规输入可写 kwargs，任意字符串键使用 `**dict`。不根据节点名生成 Python 属性或函数。

`node()` 默认 `return_call=False, declared_outputs=True`，直接给出输出，适合普通 Python 组合：

```python
model, clip, vae = remote.node("CheckpointLoaderSimple", ckpt_name=checkpoint)
positive = remote.node("CLIPTextEncode", text=prompt, clip=clip)
```

| 声明输出数 `D` | 默认 `node()` 返回 | 等待默认返回值 |
| --- | --- | --- |
| `0` | `None` | 需要等待时改用 `return_call=True` |
| `1` | 一个 `OutputFuture`；capture 内为 `GraphRef` | eager 的 `await output` 给一个 `RemoteRef` |
| 多个 | 包含各槽的可等待 tuple | eager 等待后是相同槽序的 ready refs tuple |

输出可以直接作为下一个节点的输入，无需先 await。`GraphRef` 在所属 capture 内可以 await，返回同一符号引用，不执行计算；图外等待某次运行的方式见下文。多输出 tuple 可以先保存、传给普通 Python helper，再解包，规则相同。

调用时已经创建内部 invocation、完成 future 和发送项。后台 sender 完成握手/必要 artifact 上传后发送；`await` 只等待同一次调用，等待提交 ACK 不等于等待计算完成。零输出调用也会入队；需要等待、metadata、调用 ID 或控制句柄时，显式 `return_call=True` 保留 `Call`。未 await 的调用也会执行，但事件循环必须有调度机会。Client 应通过 `async with` 或显式 `await close()` 管理。

提交、结果读取和 GC 控制消息仍需要业务 loop 可运行。独立会话保活的改动只针对 lease 续约，不把阻塞的业务协程变为继续执行；HTTP keeper 的已验范围、历史暂停反例与 custom transport 限制见 [会话语义](ownership.md#会话续约与业务-loop-暂停)。

公开 Call、未 release 的 Future/Ref owner cell 与 Graph 共享保留所属 Client；helper 返回句柄后，局部 Client 变量消失不会单独关闭会话。Call 本身是独立拥有者，输出全部 release 后仍保留会话，直到 Call 被回收；当前没有 Call release API。输出 `release()` 撤销同 cell 的全部别名并交还该 cell 的会话持有，不撤销其他 Call/Graph。显式 Client close/context exit 优先撤销整个 session；最后拥有者 GC 则安排异步清理，详见 [所有权](ownership.md)。

| 对象/操作 | 含义 |
|---|---|
| `node(..., return_call=True)` | 保留一次 invocation 的 `Call`；仍遵守输出数模式 |
| `call.output(i)` | 该输出槽的 `OutputFuture`，可以尚未就绪 |
| `await call` | 默认按声明输出数给出 refs tuple；`declared_outputs=False` 时给全部实际 refs tuple |
| `await call.output(i)` | 等同一 producer token，返回一个 `RemoteRef` |
| `RemoteRef` | 已取得的远端引用，不可 await；仍可能因 session/缓存失效 |
| `await ref.fetch()` | 显式反序列化到本地，不重新执行 producer |

`.python()` 与 `.submit()` 仍返回 `Call`；它们的整体 await 返回 refs tuple。`.python()` 只有一个输出槽，函数返回 tuple/list/dict 时整个值放在此槽中，不自动拆成节点输出。零输出 `Call` 的整体结果是空 tuple，副作用仍需执行完成。

## 声明输出数与实际结果

`declared_outputs=True` 按当前节点声明提供输出槽，以 `D` 表示声明输出数。管理连接到已运行 worker 时取得 live schema 快照；`await remote.comfyui.start()`、`restart()` 或 `info()` 也更新对应计算代际的本地快照。这些仍是真实远端管理 I/O，不会为本地查询伪造一次远端计算。worker 停止或未安装时只建立管理连接，不隐式启动计算或附着计算 session。默认直接输出需要立即构包；没有可用固定声明时，调用报 `UnknownOutputLayout`。先显式 start，或对已运行实例 await info。

Eager 中显式 `return_call=True` 允许先保留 Call 并入队，sender 可在计算握手取得 schema 后固定 `D`；若节点没有固定声明，整体 await 仍报 `UnknownOutputLayout`，此时应选 `declared_outputs=False`。这不会改变立即提交、await 只等待的规则。

多输出结果包在 **解包时** 按 `D` 检查接收数 `N`。调用本身已入发送队列；普通 `a, b = remote.node(...)` 或先保存后解包都可能立即报接收数错误，此时任务可能已经下发，远端是否收到没有时序保证，也不会自动取消或撤回。`a, b = await call` 则先等执行，再由 Python 解包检查。capture 内的错误发生在提交 graph 之前。SDK 不做字节码 pre-submit 接收数预检。

声明数不是实际输出数 `M` 的验证：按声明数 await 时取得前 `D` 个真实 refs。`D > M` 时取缺槽会使 await 先报 `IndexError`，赋值尚未发生；不会制造不存在的 ready ref。`D < M` 时只投影声明的 `D` 个槽。这不是类型校验，执行器也不增加远端 `M == D` 检查。

`declared_outputs=False` 返回一个可等待的输出 proxy。普通 `a, b = proxy` 按本地 `UNPACK 2` 提前选槽 `0, 1`，不依赖声明 `D`，也不等待执行结果；星号解包不提供固定槽数。Eager 的 `await proxy` 才根据实际结果塑形：零输出为 `None`，单输出为一个 ready ref，多输出为 ready refs tuple。`return_call=True, declared_outputs=False` 保留 Call，eager 的 `await call` 始终返回全部实际 refs tuple。

Eager 中也可把未解包的 proxy 整体交给下一个节点。`remote.node("NodeB", value=proxy)` 的输入解析语义等价于：

```python
ready = await proxy
remote.node("NodeB", value=ready)
```

其中实际零输出时 `ready` 为 `None`，单输出时为一个 `RemoteRef`，多输出时为 refs tuple。daemon 等 producer 完成后沿用现有引用、容器和原生 stream 的解析规则，数据仍留在远端；单个 `OUTPUT_IS_LIST` 输出仍按原生流映射消费者。capture 中必须先固定数量解包或通过 Call 选择明确的输出槽，不能把 whole-result proxy 当作一个原生输出槽。

```python
call = remote.node("MultiOutputNode", return_call=True)
# await 完成后若接收数错误，可保留同一个 Call，按正确接收数再次 await。
a, b = await call
```

重复 await 不重新发送节点。`return_call=True` 只保留句柄，仍遵守 `declared_outputs`，不能用它绕过声明投影。

`remote.files` 是独立的 async 文件 I/O 入口。`await remote.files.upload(...)` 直接返回本地 `RemoteFile` 元数据，download 返回 `DownloadedFile`；它们不是 Call、Future 或远端对象引用，文件操作不会被 graph capture 收集。文件与模型根选择、`native_name` 及持久性见 [文件 I/O](files.md)。

HTTP fetch 保留 cloudpickle 对象语义，默认协商二进制响应。可选安装 `comani[lz4]` 后，`Client(url, fetch_compression="auto")` 在两端支持时允许 LZ4；`"identity"` 禁用该压缩。自定义 transport 的配置写在 `HTTPTransport(..., fetch_compression=...)`，再传给 Client，不能同时给 Client 该选项。协议与开销边界见 [结果取回](protocol.md#二进制结果取回)。

取消某个本地 waiter 不取消底层远端调用。`close(wait=False)` 关闭会话并令待处理 SDK 等待失败；原生正在执行的同步工作不保证立即停止，见 [Runtime](runtime.md)。

支持恢复的 HTTP daemon 上，临时传输错误自动恢复原 session/epoch 与原 call ID，不重跑脚本。默认断网宽限一小时，可配置；真实重启或已过期会话明确失败。离线 release/forget 在恢复后幂等补发，显式 close 撤销计算优先；管理任务与计算 session 独立。网络恢复不把 borrowed cache view 变成 owned，见 [生命周期](ownership.md)。

## 可选执行 metadata

`node()` 的 `return_call` 和 `declared_outputs` 是执行选项。普通 kwargs 中的 `extra_data` 仍是节点端口；需要执行 metadata，或节点恰好有名为 `return_call` / `declared_outputs` 的真实输入时，把输入 Mapping 放在第二个位置：

```python
plain = remote.node("OriginalClassName", extra_data=node_input)
call = remote.node("OriginalClassName", {"extra_data": node_input,
                                        "declared_outputs": node_count_mode},
                   extra_data=metadata, return_call=True)
run = remote.submit(workflow, inputs=bindings, extra_data=metadata)
```

第二位置 Mapping 形式的 kwargs 只允许执行选项 `extra_data`、`return_call` 和 `declared_outputs`，其他选项会报 `TypeError`。metadata 必须是 JSON 兼容 Mapping，在调用时生成快照；不能放 RemoteRef、Future 或任意 Python 对象。它传给原生执行上下文，不会变成节点端口。`submit` 的选项同时适用于 captured graph 与 raw API prompt。

构图时，单个节点或嵌套 `submit` 的非空执行 metadata 会立即报 `ValueError`；统一放到构图结束后的最外层 `remote.submit(..., extra_data=...)`。普通 capture 不增加步骤。这里的 API 节点是调用外部服务的原生后端节点类别，不是 SDK 新增的固定节点名。

完成后 `await call.metadata()` 返回普通本地 `CallMetadata(call_id, prompt_id, executed, ui)`，不需 fetch，不拥有远端对象。它只投影必要的原生 UI/history outputs，排除原 prompt、extra_data 和 history.meta；不序列化 opaque 对象/Tensor。单节点 UI key 为 `result`，workflow UI key 为实际 graph node ID。

```python
saved = remote.node("SaveImage", images=image, filename_prefix="comani/result", return_call=True)
await saved
metadata = await saved.metadata()
item = metadata.ui["result"]["images"][0]
# filename/subfolder/type 定位原生文件，随后 files.stat/download/remove。
```

SaveImage 的 PNG 是明确的 8-bit 文件输出；`fetch()` 保持完整浮点 Tensor，不隐式压成 PNG。直接取图片不需要 source wrapper 来读取 UI 或写临时文件。

## Graph receiver

```python
with remote.graph() as workflow:
    image = workflow.input("image", type="IMAGE")
    middle = remote.node("ImageInvert", image=image)
    last = remote.node("ImageInvert", image=middle)
    workflow.output("image", last)

run = remote.submit(workflow, inputs={"image": source})
early = await run[middle]
final = await run.output("image")
```

`with` 通过 ContextVar 切换当前任务的 receiver；嵌套退出和异常退出会恢复先前 receiver。scope 内调用只记录节点，默认直接输出符号 GraphRefs；保留的 Call 或多输出结果包也可 await 取得符号 refs，不执行节点或提交图。默认使用前述 schema 快照，可先在图外显式完成 start/info。声明未知时，保留的 Call 仍可在 await 时附着已运行计算进程读取 schema；这一步是 I/O，既不运行图也不自动启动计算。`declared_outputs=False` 的 proxy 在 capture 内 await 后仍是符号 proxy，可随后固定数量解包。普通 Python 此时仍在本地执行；符号值不能用于本地 bool 判断、迭代运行时数据或 fetch。

退出 scope 只封图，不提交。GraphRef 在所属 capture 内 await 返回符号自身；离开该 capture 或在其他 capture 内直接 await 会报 `NotSubmitted`，不能据此等待某次运行。图外通过 `run[GraphRef]` 将它绑定到明确的一次运行。一个 sealed graph 可以重复提交，每个 run 有独立调用 ID，端口不会隐式绑定到“最后一次运行”。

`run.output(name)` 的名称是声明输出的 slot 别名，整体 await 仍按声明顺序返回 tuple。它和 `await run` 等整个 workflow invocation，包括原生收尾；`run[GraphRef]` 等该节点 producer，可在整体结束前就绪。声明输出已经产出不代表 workflow 已完成。

## 目标、组合与控制流

声明的输出 demand 其 producer；原生 OUTPUT_NODE 可作为默认执行根，显式副作用用 `workflow.target(call)`。查询 `run[port]` 不添加执行目标，所以未被原生 lazy 分支请求的中间量会得到 `Skipped`，不会自动补算。

在外层 capture 中调用 `remote.submit(inner, inputs=...)` 会结构性嵌入子图：绑定输入、隔离节点 ID、重映射连接与输出，不发送独立 RPC。它不做全程序捕获、跨实例分配或全局优化。

相邻的两个 graph 内 await 不增加 A→B 顺序边；只有输出传入输入才形成数据边。数据相关条件、循环和动态展开使用原生节点表达；本地已知参数可用普通 Python if/for 决定构图结构。

默认直接输出的 metadata 未知时，调用报 `UnknownOutputLayout`；可用 `declared_outputs=False` 固定槽数解包，或保留 `return_call=True, declared_outputs=False` 后显式 `call.output(slot)`。显式 pending slot 不被旧 metadata 快照提前限制。

## 旧 Call 写法迁移

| 原写法 | 常规直接输出写法 |
| --- | --- |
| `a = remote.node(...); next_node(value=a.output())` | `a = remote.node(...); next_node(value=a)` |
| `a = remote.node(...); next_node(x=a.output(0), y=a.output(1))` | `x, y = remote.node(...); next_node(x=x, y=y)` |
| `result, = await remote.node(...)`（单输出） | `result = await remote.node(...)` |
| 需要 `id`、`metadata()`、graph target 或同一调用的重复 await | `call = remote.node(..., return_call=True)` |

现有 `.python()` / `.submit()` 的 `Call.output()` 和 refs 所有权写法保持适用。

## 原生 API prompt

`remote.submit(api_prompt, outputs={"image": ["decode", 0]}, targets=["decode"])` 接受后端 `node_id -> {class_type, inputs}` 格式。它保留原生 `[node_id, slot]` 链接，不是前端 canvas JSON。外部 Python 内存对象和远端引用通过 capture 的 input/bindings 传入，不直接塞进标准 JSON。
