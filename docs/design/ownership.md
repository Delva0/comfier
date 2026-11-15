# 远端对象、GC 与会话

实现：[SDK refs](../../src/comani/refs.py)、[client 控制消息](../../src/comani/client.py)、[ObjectStore](../../src/comani/_runtime/objects.py)、[runtime reservations](../../src/comani/_runtime/service.py)。

## 两种所有权

| 来源 | 远端持有者 | 本地句柄消失后 |
|---|---|---|
| Eager node/Python 输出 | 会话的 owned lease | 最后一个本地 owner 消失后发送 release |
| Workflow 输出/中间量 | 原生 ComfyUI cache；SDK 是 borrowed view | 不保活原生 cache；定位 metadata 可被回收 |

有效会话内的 owned lease 使本地保留的 eager 输出能继续被调用，保持逻辑对象可用；它不表示对象固定驻留 GPU，也不取代 ComfyUI 的显存/模型管理。释放 lease 不保证显存立即下降：原生缓存、其他输出、正在执行的消费者或用户 Python 全局变量可能仍引用同一对象。

Borrowed ref 绑定 cache entry 的身份及版本。entry 被驱逐后，即使相同 cache key 后来重新出现，旧 ref 也报 `Expired`，不会自动指向新值或重算 producer。

Owned 与 borrowed 句柄都保留远端定位所需的 Client 会话能力，但只有 owned lease 保活相应远端结果；borrowed 句柄保留 Client 不会 pin 原生 cache。

文件不是第三种对象 lease。已完成的原生文件由文件系统持有，GC、release、Client close 不删除它。`remote.files` 代理强持有 Client；返回的 `RemoteFile`、`FileRoot`、`DownloadedFile` 只是本地元数据，不保活会话。显式删除与传输提交点见 [文件 I/O](files.md#持久性与会话)。

## 本地持有链

```text
公开 Call ───────────────────────────────→ Client 会话
公开 Graph / GraphRef / GraphInput → Graph → Client 会话
OutputFuture / RemoteRef 别名 → 共享 owner cell → Client（未 release 时）
owner cell → 轻量 producer state → invocation lifetime
内部 state、cell/Call 回边及 finalizer 参数保持弱引用
```

删除局部 Client 变量只减少一个本地别名；公开 Call、未 release 的输出 cell 或 Graph 仍是合法会话拥有者。Call 独立保留 Client，直到 Call 被回收，即使其全部输出已 release；当前没有 `Call.release()` API。Graph 及其端口沿既有 Graph→Client 关系保留会话。

相同逻辑输出的 Future 和 ready Ref 别名共享 owner cell。`ref.release()` / `future.release()` 幂等地撤销该 cell 的全部别名，并交还该 cell 对 Client 的强持有；它不撤销其他输出、Call 或 Graph 的独立拥有权。Output 不反向保留整个 Call，所以只保留一个输出不会因此保活其他槽的数据。

最后一个公开拥有者消失后，Client finalizer 停止 keeper，并异步安排普通 transport/session 清理。keeper 退出不表示 close RPC 已到达或普通 HTTP 池已同步关闭；后者仍需要业务 loop 进展。逐输出 release 可能被整个 session 的 close 或 TTL 回收覆盖，没有逐条 release 的发送顺序保证。不要依赖某一行 `del` 立即完成网络清理或释放 VRAM。

内部 state/lifetime、ready-ref/Call 回边和 finalizer 参数不另持有 Client。SDK 自有长期任务、Future/done callbacks、GC 控制队列与 keeper 线程使用空 Context，避免复制应用 ContextVar 中的 Client/Graph 而形成隐藏拥有者。用户主动 fetch/close/await、用户创建的 task 和回调仍保留用户上下文；这些合法持有不属于内部泄漏。

## ACK 与正在消费的输入

SDK 在发送/上传及提交 ACK 前强引用输入。ACK 后 daemon 已为输入登记 reservation；未完成 producer 的输出也能先登记。消费者结束、失败或取消时释放 reservation，避免输入在传输与排队期间被本地 GC 提前删除。

对于 borrowed 输入，已接受消费者可以短暂保留消费所需的 snapshot，直到其执行结束。普通 borrowed view 本身仍不保活 cache，其 `fetch()` 仍检查原生 cache 是否有效。这不是对原始 workflow ref 的自动所有权升级。

需要明确保留 workflow 结果时，可以提交一个 eager identity 消费者：

```python
run = remote.submit(workflow, inputs=bindings)
kept = remote.python(lambda value: value, inputs={"value": run.output("image")})
owned_image = await kept.output(0)
```

消费者必须在源值尚可使用时被接受；已经失效的 ref 无法通过此方式复活。新 eager 输出有自己的 owned lease，原始 workflow ref 仍是 borrowed。

## 调用记录与会话兜底

一个 invocation 的全部本地 Call/Future/Ref state 共享 lifetime token。会话仍由公开拥有者保留时，后台 watcher 在计算结束前也持有该 token，临时 Call 被丢弃不会单独取消 fire-and-forget。watcher 本身不成为 Client 的公开拥有者；最后所有公开拥有者消失时，session 整体清理兜底，不为无人持有的会话一直续租。最后一个 invocation token 消失后，SDK 批量发送 `forget_calls`。

daemon 仅在调用终态、非 inflight、无依赖 reservation 时回收调用记录和输出定位 metadata；这不会 pin 或清空原生 cache。失败结果保存为 wire 数据，避免长期保存原生 exception traceback 与其中的模型对象。

对象/call 记录的回收与关闭会话诊断记录是不同层。握手由服务器签发 session ID；授权只查询 active session 表，关闭后立即移除活跃记录与 seen 时间。已关闭 ID 仅保留最多 64 项诊断记录，不参与授权；旧 ID 不能通过 heartbeat/submit 重新创建会话。[当前构建的独立报告](../../tests/stress/CURRENT_HEAD_PRESSURE_REPORT.md) 中，1,000 会话短 TTL churn 的诊断记录保持在 64 项；32-session 运行逐个确认 session 在 close 入口仍有效、calls/objects 已归零，且没有活跃会话意外失效。关闭中的空会话可以由 TTL 兜底回收；不能仅凭最终表为空就推断是 GC 回收。基线与 v2 的失败记录仍保留。

## 会话续约与业务 loop 暂停

当前默认 session TTL 为 120 秒，续约间隔最长 20 秒；正在进行的 HTTP 等待也计入活跃请求。HTTP transport 为每个有效会话启动专用 keeper 线程，使用自己的 event loop 和 aiohttp 连接续约，不依赖业务 asyncio loop 的调度。独立 CPU 验证在 0.6 秒 TTL 下阻塞业务 loop 2.4 秒，期间收到 12 次 heartbeat；原 Call/ownedRef 恢复后仍能 fetch 并继续作为输入使用。

临时心跳连接错误只表示续约暂不可达，不会在本地抢先拒绝正常业务 RPC；真实 fetch/submit 的网络错误和服务器返回的失效错误仍向调用方传播。确认的终态 lease 错误会停止使用原会话，不重新握手复活或重提任务。v2 的 transient 误拦反例及 v3 的确定性修复复验保存在 [验收记录](../../tests/acceptance/HEARTBEAT_REPORT.md)。InProcessTransport 和其他 custom transport 的 fallback heartbeat 仍共享业务 loop。

独立续约也依赖客户端进程能够运行及真实网络可达。进程/整机冻结、网络中断或 daemon 重启仍可能导致 lease expiry；它不是任意 GIL/GC 停顿、OS 暂停或业务继续提交的保证。真正已过期的 session 不重新握手复活，引用不补算，副作用不重提。

显式 `async with Client(...)` 退出或 `await client.close()` 主动撤销整个会话，存活的 Call/Ref/Graph 不能阻止关闭。关闭后的包装对象可保留本地已完成 metadata，但不能继续成功进行远端操作。应在业务 loop 关闭前完成显式关闭；owner loop 已关闭时 keeper 会退出，但该 loop 上的普通 transport 异步清理无法再执行。

`close(wait=False)` 先使逻辑等待失败，原生任务可能仍在执行；临时 reservations 在原生结束后才完成清理。目录包的标准 Python 模块及其 globals 是进程级状态，不属于 session close 卸载的范围，见 [Python artifacts](python-artifacts.md)。

## 失效不会重算

`Released` 表示本地 owner 已显式释放；`Expired` 表示远端/session/cache 引用不可再用；`Skipped` 表示原生分支未产生结果。`fetch()` 或等待引用都不会为这些状态启动另一次计算。
