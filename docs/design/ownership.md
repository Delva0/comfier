# 远端对象、GC 与会话

实现：[SDK refs](../../src/comani/refs.py)、[client 控制消息](../../src/comani/client.py)、[ObjectStore](../../src/comani/_runtime/objects.py)、[runtime reservations](../../src/comani/_runtime/service.py)。

## 两种所有权

| 来源 | 远端持有者 | 本地句柄消失后 |
|---|---|---|
| Eager node/Python 输出 | 会话的 owned lease | 最后一个本地 owner 消失后发送 release |
| Workflow 输出/中间量 | 原生 ComfyUI cache；SDK 是 borrowed view | 不保活原生 cache；定位 metadata 可被回收 |

Owned lease 使本地保留的 eager 输出能继续被调用；它不取代 ComfyUI 的显存/模型管理。释放 lease 也不保证显存立即下降：原生缓存、其他输出、正在执行的消费者或用户 Python 全局变量可能仍引用同一对象。

Borrowed ref 绑定 cache entry 的身份及版本。entry 被驱逐后，即使相同 cache key 后来重新出现，旧 ref 也报 `Expired`，不会自动指向新值或重算 producer。

## 本地持有链

```text
用户持有 Call → 每个输出槽的 OutputFuture
用户持有 OutputFuture / RemoteRef 别名 → 同一槽的 owner cell
owner cell → 轻量 producer state → invocation lifetime
Client 的 state 表只持弱引用
```

相同逻辑输出的 future 和 ready 别名共享 owner cell，只释放一次。Output 不反向保留整个 Call；只保留一个槽时，不会因此保活其他槽。用户主动保留 Call，则 Call 可以继续提供全部 outputs，其持有链需要一起释放。

GC finalizer 只排队控制消息，不在 GC 中等待网络；显式 `ref.release()` 是幂等补充，并使该 owner cell 的本地别名不可再用。不要依赖某一行 `del` 立即完成网络或释放 VRAM。

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

一个 invocation 的全部本地 Call/Future/Ref state 共享 lifetime token。后台执行 watcher 在计算结束前也持有它，所以 fire-and-forget 不会因临时 Call 被丢弃而取消。最后一个 token 消失后，SDK 批量发送 `forget_calls`。

daemon 仅在调用终态、非 inflight、无依赖 reservation 时回收调用记录和输出定位 metadata；这不会 pin 或清空原生 cache。失败结果保存为 wire 数据，避免长期保存原生 exception traceback 与其中的模型对象。

对象/call 记录的回收与关闭会话诊断记录是不同层。握手由服务器签发 session ID；授权只查询 active session 表，关闭后立即移除活跃记录与 seen 时间。已关闭 ID 仅保留最多 64 项诊断记录，不参与授权；旧 ID 不能通过 heartbeat/submit 重新创建会话。[独立 session churn 复验](../../tests/stress/INDEPENDENT_LOAD_REPORT.md) 已通过 100→300→1,000 会话及额外 14 个 TTL 的观察：活跃记录、calls/objects/leases/requests 均归零，诊断记录保持 64。旧 ID 淘汰、fresh handshake 与 inflight close 也已单独复测；结论不限于 owned 对象计数。

SDK 后台 heartbeat 续约会话；正常 close 立即清理，断线后由 session TTL 兜底。正在进行的 HTTP 等待也被计入活跃请求，长模型执行不因为缺少新提交而自动失去会话。当前默认 TTL 为 120 秒，SDK heartbeat 最长间隔 20 秒。

`close(wait=False)` 先使逻辑等待失败，原生任务可能仍在执行；临时 reservations 在原生结束后才完成清理。目录包的标准 Python 模块及其 globals 是进程级状态，不属于 session close 卸载的范围，见 [Python artifacts](python-artifacts.md)。

## 失效不会重算

`Released` 表示本地 owner 已显式释放；`Expired` 表示远端/session/cache 引用不可再用；`Skipped` 表示原生分支未产生结果。`fetch()` 或等待引用都不会为这些状态启动另一次计算。
