# RPC、值与事件协议

实现：[protocol.py](../../src/comani/protocol.py)、[transport.py](../../src/comani/transport.py)、[HTTP app](../../src/comani/_runtime/http.py)、[errors.py](../../src/comani/errors.py)。当前协议版本常量为 `1`。

## RPC

SDK 使用 `POST /rpc`，请求为 `{"method": "...", "params": {...}}`；成功返回 `{"result": ...}`，失败返回 `{"error": {"code": "...", "message": "..."}}`。SDK 先解析协议错误，再处理 HTTP 状态，因此 HTTP 400 上的 `Expired` 等仍有正确类型。

| Method | 作用 |
|---|---|
| `handshake` | 服务器新签发 session ID、runtime epoch、Python/cloudpickle 版本、原始节点 metadata、session TTL 与 capabilities |
| `artifact_put` | 按 SHA256 上传代码 artifact |
| `submit` | 登记 `session_id/call_id/spec` 与输入 reservations，返回接受状态 |
| `wait_result` | 等整个 invocation；可指定 `node_id` 等 workflow 中一个 producer |
| `fetch` | 按 ref ID/epoch 取回显式请求的 Python 值 |
| `release` | 幂等释放 owned lease/view 记录 |
| `forget_calls` | 回收不再持有的终态调用 metadata，活跃依赖会延后回收 |
| `heartbeat` | 更新会话活跃时间 |
| `close_session` | 结束会话并清理相应资源 |

当前 HTTP transport 使用三个有界连接池：控制请求 32、结果长轮询 100、artifact/fetch 数据传输 4。长轮询和大数据请求不占用提交、heartbeat、release、close 的连接额度。连接池界限是 transport 的资源边界，不代表 native worker 的并行计算数。

这些是业务 RPC 的连接池。HTTP keeper 另外使用自己线程/event loop 中的一条专用连接，使业务 loop 暂停时仍能续约，见 [会话续约](ownership.md#会话续约与业务-loop-暂停)。暂态 HTTP 状态、连接错误或截断响应不等于 lease 已关闭，不会抢先拒绝真实业务 RPC；明确终态和完整协议错误仍拒绝复用，业务 RPC 自身的错误照常传播；`heartbeat` 只更新有效 session，不重新握手或重新提交任务来恢复已过期会话。

### 经过反向代理的等待

服务器通过 `capabilities.bounded_wait=true` 声明有界长轮询。SDK 随后为 `wait_result` 发送 `wait_seconds=20`；在这段时间内尚未终态时，服务器返回 `{"pending": true}`，SDK 用相同 session/call/node ID 继续读取。超时只结束本次读取，不取消 producer，也不会重提计算。终态仍返回 outputs 或原有错误。服务器接受大于 0、不超过 60 的有限秒数；未指定时保留旧的无限等待语义。

旧服务器没有声明能力时，SDK 不发送新参数，仍使用单次等待。这个兼容机制避免一般反代的单请求读取超时；连接中断、代理拒绝或其他传输错误仍会正常暴露，不能被当成 `pending` 无限重试。

临时 Cloudflare Quick Tunnel 另有 200 个并发在途请求限制。结果等待会占用其中的额度，即使每轮只有 20 秒；客户端连接池分离不能突破代理总限额。需要按实际部署控制并发等待量，或选用具备合适容量的入口；核心 RPC 不依赖某个代理产品。

[当前安装复测](../../tests/stress/CURRENT_HEAD_PRESSURE_REPORT.md) 记录真实 native 调用运行 140.010 秒，经过 125 秒读取超时代理，一次 submit、六次 pending、十次查询/终态复读及稳定的最终引用，并检查取消与 close 前清理。这段 140 秒测量绑定 885b8cd 基线；当前 HTTP 独立保活的压力与生命周期结果另见 [启动与保活验收](../../tests/acceptance/HEARTBEAT_REPORT.md)。历史[真实远端 HTTPS 验证](../../artifacts/remote/60662518-20261114/long-wait.json) 的 130 秒调用及完整模型结果另见 [远端报告](../remote_acceptance.md)。

## 调用与引用

单节点 spec 包含 `kind=node`、原始 `node_type` 和编码后的 inputs。Workflow spec 包含节点、输入端口、声明 outputs、执行 targets、bindings 及是否为原生 prompt JSON。

两类提交均可携带可选 JSON `extra_data` 执行 metadata，与节点 inputs 分开。SDK 的 [显式输入 Mapping 形式](sdk.md#可选执行-metadata) 保证同名真实节点端口不被执行选项占用；capture 中的 metadata 只在最外层提交时提供。

输出 metadata 包含 opaque `id`、runtime `epoch`、`owner=owned|borrowed`。输入引用通过 producer locator 传递：session、epoch、call ID、slot，以及可选 workflow node ID。对象不通过文件名或 Python 内存地址跨进程访问。

SDK 采用握手返回的服务器 session ID，首批调用在握手完成后才编码引用。服务器仅从 active 表授权会话；未知/已关闭 ID 不会被普通 RPC 重新注册。最近关闭的最多 64 个 ID 只用于诊断，内部 raw/prompt 频道也不能作为用户 RPC 会话使用。

引用只属于其会话/runtime；跨 Client、跨实例或重启后的 epoch 不会被隐式搬运。若要在本地或另一个进程使用真实对象，需要显式获取/传输并满足 codec 的环境要求。

## 容器与原生连接

普通 list、tuple、dict 分别使用 `$list/$tuple/$dict` 标签，opaque 本地值使用 `$value`。引用为 `$ref`，graph 内端口为 `$graph_ref`，声明输入为 `$input`。因此 SDK 中普通 `["node-id", 0]` 保持普通列表，不被误认成图边。

仅在原生 graph lowering 时，内部端口才转为 ComfyUI `[node_id, slot]`；raw API prompt 已处于这种语言中。外部 refs 先解析为对象，再由内部 BoundInput/cache-provider 供执行器读取。

Python args/kwargs 的容器结构保存在 layout 中，叶值提升为 `v0/v1/...` 原生端口，layout 用 `$port` 引用它们。这样嵌套的 Future/Ref 不会被 cloudpickle 暗中复制为模型或 Tensor 数据。

## 状态、错误与重试

本地入发送队列、远端接受、计算完成是三个时间点。Client 的 Call 状态与 daemon 的 admission/native 状态不是一份完整设备监控面板。

标准错误包括 `NotSubmitted`、`InvalidReference`、`Released`、`Expired`、`Skipped`、`SessionClosed`、`CodeCompatibilityError`、`PackageConflict`。其他远端错误映射为 `RemoteError`，原 code/message 留在 details 中。

同 session 的 call ID 用于在仍保留记录时识别重复提交；本实现不提供持久化任务日志，也不承诺跨进程崩溃 exactly-once。丢失响应不能被解释为副作用未发生。

## 辅助 HTTP 与事件

driver 还提供 `/prompt`、`/history`、`/queue`、`/ws` 的小型辅助接口。`/prompt` 支持后端 prompt 和原生目标校验，history 可观察后端 UI 输出；它不是完整 ComfyUI server/WebUI 的替代品，`/view`、canvas 转换等路由未实现。

事件有 `accepted/queued/running/output_ready/completed/failed`，原生消息放入 `native` envelope，带 session/call ID。二进制预览以 JSON-safe base64 表达，不等同于所有官方前端客户端的原始帧格式。

内置 SDK 通过 RPC 长轮询完成等待，不依赖事件流推导终态；事件目前用于外部观察和辅助接口，SDK 没有伪装成完整的原生 WebSocket client。

## 网络入口

配置 token 时使用 Bearer 鉴权；无 token 时仅接受 loopback peer、local Host 与同源浏览器请求。代码执行属于授权能力，实际远端网络、转发和鉴权部署应以独立远端报告为准。
