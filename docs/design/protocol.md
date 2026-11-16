# RPC、值与事件协议

实现：[protocol.py](../../src/comani/protocol.py)、[transport.py](../../src/comani/transport.py)、[管理 HTTP](../../src/comani/daemon/management_http.py)、[计算 HTTP](../../src/comani/daemon/http.py)、[errors.py](../../src/comani/errors.py)。基础协议版本常量为 `1`，新增能力以精确 capability 版本协商。

## 管理与计算通道

Client先用`POST /manage`管理握手，获得daemon epoch、管理身份、文件epoch与有效policy，不启动ComfyUI。`management_read`执行真实远端查询；`management_window/begin/status/result/logs/cancel`管理独立Operation。首个计算调用才通过公开`/rpc`惰性附着已经启动的runtime；计算session/runtime epoch与管理身份/daemon epoch分开。

管理daemon将计算RPC、binary/LZ4、辅助HTTP和WS按有界原始字节流代理到loopback子进程，不解码Tensor。文件流由父进程自身处理；计算重启后Files继续使用management_handshake的file epoch，不误用新计算epoch。认证和公网绑定规则在所有入口保持一致。

## RPC

SDK 使用 `POST /rpc`，请求为 `{"method": "...", "params": {...}}`；成功返回 `{"result": ...}`，失败返回 `{"error": {"code": "...", "message": "..."}}`。SDK 先解析协议错误，再处理 HTTP 状态，因此 HTTP 400 上的 `Expired` 等仍有正确类型。

| Method | 作用 |
|---|---|
| `handshake` | 计算服务器签发 session ID、runtime epoch、恢复授权、Python/cloudpickle/节点 metadata、宽限和 capabilities |
| `resume` | 验证原 session、恢复授权及同 runtime epoch，恢复仍有效的原会话 |
| `artifact_put` | 按 SHA256 上传代码 artifact |
| `submit` | 登记原 session/call ID、spec digest、签名窗口和输入 reservations，返回接受状态 |
| `submit_batch` | 协商后将已排队调用按原顺序交给逐项 submit，返回各自接受/失败回执 |
| `wait_result` | 等整个 invocation，并带必要 JSON metadata；可指定 `node_id` 等 workflow 中一个 producer |
| `fetch` | 按 ref ID/epoch 取回显式请求的 Python 值 |
| `file_roots` / `file_stat` / `file_remove` | 显式文件根、元数据和删除；不产生远端对象引用 |
| `release` | 幂等释放 owned lease/view 记录 |
| `forget_calls` | 回收不再持有的终态调用 metadata，活跃依赖会延后回收 |
| `heartbeat` | 更新会话活跃时间 |
| `close_session` | 结束会话并清理相应资源 |

当前 HTTP transport 使用三个有界连接池：控制请求 32、结果长轮询 100、artifact/fetch/file 数据传输 4。长轮询和大数据请求不占用提交、heartbeat、release、close 的连接额度。连接池界限是 transport 的资源边界，不代表 native worker 的并行计算数。

这些是业务RPC连接池。HTTP keeper另用独立线程/event loop中的专用连接保活。支持恢复时暂态故障触发同session/epoch恢复；终态协议错误明确失败。heartbeat只更新有效session，不能恢复已过期会话。只有认证客户端接触更新seen，内部worker/排队或僵死TCP不续约。默认宽限3600秒，见[会话语义](ownership.md#会话续约与业务-loop-暂停)。

### 经过反向代理的等待

服务器精确声明整数 `capabilities.submit_batch=1` 时，SDK 会把当前发送队列中可下发的有序前缀合为 `submissions` 列表；后项需要上传新代码 artifact 时先下发已有前缀，不等待后项上传。不增加收集等待，不捕获/编译 graph，也不等待输入 Future ready。每项仍有原 call_id、spec 和签名窗口，daemon 复用原 submit 按序受理，某项失败不会阻止后续独立项。回执不是批次事务；整批或仅前缀已受理后丢失 ACK，仍以原每项身份/内容/窗口重放，既有 digest、strict expiry、reservations 与 owned/borrowed 规则不变。每个 envelope 最多64项/8MiB，超出会拆包，单个较大调用仍走原 submit；这些是传输分帧界限，不改变逻辑调用配额。跨包及发送期间新入队的调用仍等前一包 ACK，不是全程 streaming。未声明该版本、包括布尔 true 的旧/不同 daemon，继续原逐条路径。结果观察仍逐 call 并行进行。

服务器通过 `capabilities.bounded_wait=true` 声明有界长轮询。SDK 随后为 `wait_result` 发送 `wait_seconds=20`；在这段时间内尚未终态时，服务器返回 `{"pending": true}`，SDK 用相同 session/call/node ID 继续读取。超时只结束本次读取，不取消 producer，也不会重提计算。终态仍返回 outputs 或原有错误。服务器接受大于 0、不超过 60 的有限秒数；未指定时保留旧的无限等待语义。

旧服务器没有声明能力时不发送新参数，仍使用单次等待。bounded-wait避免一般反代的单请求读取超时；真正断线由独立的恢复协议处理，不把任意错误当pending。恢复只重用原身份观察，不重提另一计算。

临时 Cloudflare Quick Tunnel 另有 200 个并发在途请求限制。结果等待会占用其中的额度，即使每轮只有 20 秒；客户端连接池分离不能突破代理总限额。需要按实际部署控制并发等待量，或选用具备合适容量的入口；核心 RPC 不依赖某个代理产品。

[当前安装复测](../../tests/stress/CURRENT_HEAD_PRESSURE_REPORT.md) 记录真实 native 调用运行 140.010 秒，经过 125 秒读取超时代理，一次 submit、六次 pending、十次查询/终态复读及稳定的最终引用，并检查取消与 close 前清理。这段 140 秒测量绑定 885b8cd 基线；当前 HTTP 独立保活的压力与生命周期结果另见 [启动与保活验收](../../tests/acceptance/HEARTBEAT_REPORT.md)。历史[真实远端 HTTPS 验证](../../artifacts/remote/60662518-20261114/long-wait.json) 的 130 秒调用及完整模型结果另见 [远端报告](../remote_acceptance.md)。

### 二进制结果取回

`fetch` 仍使用同一 `POST /rpc`，返回同一份 cloudpickle 对象字节。握手精确声明整数 `capabilities.binary_fetch=1` 时，新 HTTP 客户端发送 `Accept: application/vnd.comani.pickle`，服务端以该 media type 返回 pickle body。没有能力时使用原有 `{"codec":"pickle","data":"<base64>"}` JSON envelope；新服务端对旧客户端仍返回 JSON，新客户端也能读取 JSON fallback。能力版本不匹配或布尔 `true` 不算支持。

可选 `comani[lz4]` 在双方都有 LZ4 且服务端声明整数 `binary_fetch_lz4=1` 时，允许默认 `fetch_compression="auto"` 发送 `X-Comani-Accept-Codec: lz4-frame-v1`。`"identity"` 禁用该协商；缺 extra 或对端能力时保留 binary identity。`auto` 只选择可用 codec，不测网络带宽，也不保证更快。

服务端仅对至少 1 MiB 的完整 pickle body 尝试 fast LZ4 frame，实际节省至少 1% 才返回 `application/vnd.comani.pickle+lz4`，并以 `X-Comani-Uncompressed-Length` 声明原长度。LZ4 是独立 vendor media type，不使用 HTTP `Content-Encoding`。客户端要求一个完整、带 content checksum 的 frame，验证 frame 长度、声明长度和无尾随数据；未协商、截断、校验或长度错误直接失败，不重新请求或重做计算。

二进制 fetch 仍整块序列化、读取和反序列化，不是 tensor 专用或有损 codec。压缩还会暂时分配完整候选 body，即使高熵内容最终回退 identity；开销与实测范围见 [性能记录](../performance.md#二进制与压缩的证据边界)。

### 文件流

握手以整数 `capabilities.files=1` 声明文件接口。roots/stat/remove 使用 RPC；upload/download 使用相同鉴权与 SDK session 的 `POST /files/upload`、`POST /files/download`，共享数据连接池。内部 raw session 不允许文件 I/O。

upload 为按顺序的 `metadata` JSON、`file` 二进制、`verified` JSON 三个 multipart part；尾部带实际 size/SHA256，单份元数据最多 64 KiB。download 返回 `application/vnd.comani.file-v1`：4 字节大端元数据长度、JSON 元数据、声明大小的文件 bytes、4 字节大端 footer 长度、含 size/SHA256 的 JSON footer。客户端拒绝 footer 后的额外数据，校验成功后才发布文件。

以上multipart/framed为旧无resume能力的单次兼容路由，保持失败清理。声明整数files_resume=1时使用独立transfer registry：window/start/status/append/commit/data/verify/cancel，SDK自动维护原daemon epoch/稳定ID/签名窗口、确认offset、源revision、whole SHA和原子发布。已完成commit失去回复返回原receipt；未知/过期身份和daemon换代不重新创建。下载Range必须精确206/Content-Range；普通成功路径单遍读，恢复才可能重读prefix或全文件verify。资源、取消和持久性见[文件I/O](files.md#自动字节续传)。

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

每次计算提交在首次发送前固定call ID、规范化请求摘要和原签名窗口。同ID/同摘要回原调用，改参数冲突；forget后仍保留覆盖窗口的轻量墓碑，过期未知请求不执行。恢复原wait/fetch与离线release/forget，不重跑SaveImage/Python副作用。实际runtime重启使旧refs过期；borrowed仍依赖cache。

管理Operation另有daemon epoch/key/digest/submission window，接纳前登记身份。默认300秒限制未知首次接纳，已知key在retained结果期仍附着原操作；receipt或结果过期明确失败。容量满拒绝新接纳，不逐出窗口内去重事实，见[管理Operation](remote-management-plan.md#operation-执行与观察)。两种机制都不是持久化数据库或跨进程崩溃exactly-once。

## 辅助 HTTP 与事件

driver 还提供 `/prompt`、`/history`、`/queue`、`/ws` 的小型辅助接口。`/prompt` 支持后端 prompt 和原生目标校验，history 可观察后端 UI 输出；它不是完整 ComfyUI server/WebUI 的替代品，`/view`、canvas 转换等路由未实现。

事件有 `accepted/queued/running/output_ready/completed/failed`，原生消息放入 `native` envelope，带 session/call ID。二进制预览以 JSON-safe base64 表达，不等同于所有官方前端客户端的原始帧格式。

内置 SDK 通过 RPC 长轮询完成等待，不依赖事件流推导终态；事件目前用于外部观察和辅助接口，SDK 没有伪装成完整的原生 WebSocket client。

## 网络入口

配置 token 时使用 Bearer 鉴权；无 token 时仅接受 loopback peer、local Host 与同源浏览器请求。代码执行属于授权能力，实际远端网络、转发和鉴权部署应以独立远端报告为准。
