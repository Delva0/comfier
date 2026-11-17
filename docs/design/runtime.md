# Driver 与原生 runtime 边界

实现：[CLI](../../src/comfier/cli/)、[supervisor](../../src/comfier/daemon/supervisor.py)、[service.py](../../src/comfier/daemon/service.py)、[adapter.py](../../src/comfier/daemon/adapter.py)。

## 启动边界

`comfier daemon` 与 `python -m comfier daemon` 启动独立管理进程，配置一个 ComfyUI 子进程。`--no-start` 只启动管理，ComfyUI 目录可以不存在；CLI 不加该参数时会尝试启动计算。Client 连接本身只握手管理，不安装、不启动。环境创建、工具准备、安装与生命周期通过显式 [管理 API](remote-management-plan.md) 执行。公开入口统一经过 `cli/` 调用 `daemon/`；原生子进程使用 daemon 内部 bootstrap，不另提供启动命令。

管理解释器与 `runtime_python` 可分离。daemon 仅复制当前 comfier 包作为隔离桥接，不把管理 site-packages/PYTHONPATH 注入目标；目标依赖通过显式 prepare_runtime 准备。start 检查依赖并报错，不执行 pip。计算启动失败不会杀死管理；info/config/logs、bash 和文件 I/O 仍可用。

原生计算参数通过 `--` 传入，在原生模块初始化前应用。这个入口运行 headless backend，没有再启动第二个 ComfyUI HTTP/WebUI 服务；原生 parser 的语法覆盖范围不等于 driver 的实现范围。不支持的服务或 cache 模式必须明确拒绝，不能静默接受后忽略。

### 原生参数的实际边界

设备环境在 Torch 首次导入前设置；精度、显存 reserve、smart-memory 等计算策略交给所选 backend。CLASSIC、LRU、RAM_PRESSURE cache 及 RAM 阈值遵循原生设置，`--high-ram` 沿用原生别名语义。base/models 路径在 `folder_paths` 首次导入时应用；额外模型 YAML、input/output/user 路径、preview、自定义节点治理、prestartup 与 backend 节点初始化复用原生初始化流程。指定 temp 目录时，实际临时路径按原生规则为 `<指定目录>/temp`。

API/partner 后端节点不会被 driver 永久禁用；`--offline`、`--disable-api-nodes` 沿用所选 backend 的节点治理。节点依赖和所需执行 metadata 仍由调用方提供，见 [SDK metadata](sdk.md#可选执行-metadata)。这不增加 WebUI、前端 JS 或第三方 HTTP route 的兼容承诺。

当前保留 `NoAssets` headless 边界。下列 **`--` 后的原生选项** 被明确拒绝，作为参数错误在 Torch 初始化前结束；Comfier 自己的 `--host/--port` 仍可用：

| 不提供的服务/行为 | 拒绝的原生选项 |
|---|---|
| 第二个原生 HTTP/TLS/CORS 服务 | `--listen`、`--port`、`--tls-keyfile`、`--tls-certfile`、`--enable-cors-header`、`--max-upload-size`、`--enable-compress-response-body` |
| 原生前端与浏览器启动 | `--auto-launch`、`--disable-auto-launch`、`--front-end-version`、`--front-end-root`、`--multi-user`、`--dont-print-server` |
| Manager、agent、asset/database 服务 | `--enable-manager`、`--enable-manager-legacy-ui`、`--disable-manager-ui`、`--enable-agent`、`--enable-assets`、`--enable-asset-hashing`、`--database-url` |
| 独立测试/打包/挂起模式 | `--quick-test-for-ci`、`--windows-standalone-build`、`--debug-hang` |

另行拒绝 `--cache-none`：当前对象绑定需要 cache provider，原生 NullCache 不提供它。以上不是覆盖所有原生参数的 allowlist，也不承诺未来 ComfyUI 新参数自动兼容；更换 backend 后需要验证对应选项及节点。

## 一个设备入口

一个管理 daemon 独立监督一个 ComfyUI 子进程；子进程内包含计算 HTTP loop 和一个 native worker 线程，持有原生执行器、Tensor/模型、对象表与计算会话。daemon 不是连接任意已有 WebUI 进程后直接取得其 Python 对象。

一个公开 endpoint 有界代理计算 HTTP/RPC、二进制/LZ4 和 WS 原始字节到 loopback 子进程。管理不解码 Tensor；文件和模型获取在父进程。维护锁协调 core/插件/环境变更与启动，修改前显式停止计算。stop/restart 默认 `wait=True`，等待计算和本实例受管安装收尾；显式 `wait=False` 取消相关受管维护与待启动控制，待其进程组清理、锁释放后立即中断 runtime，再停止或尝试重启。中断不保证回滚或重启成功，partial/启动错误可查；`wait=True, force=True` 拒绝。控制不排在计算或普通管理 worker 队列之后。bash 独立执行，不持维护锁，files/模型下载也不属于紧急控制的取消范围。

live runtime 与冷路径 helper 都把实际 `custom_nodes` 多根顺序放在私有路径快照中，供插件适配器使用。该信息不进入公开 files/model 类别；包在多个配置根中重复时，变更拒绝歧义。Manager CLI 在目标子进程先复用相同原生路径配置，再运行其官方入口，安装仍使用配置顺序的默认根。

同机可以部署多个实例，但各自需要进程、端口和对象命名空间；一个使用多 GPU 的原生实例仍只有一个 owner。具体多卡计算能力取决于 ComfyUI 配置与节点，不由 SDK 的 async 或引用接口产生。

## 接收与计算分开

```text
SDK 调用 → 本地发送队列 → submit ACK
                              │
                         各调用等待自己的输入
                              │
                         就绪执行队列
                              │
                     单 native worker 逐项执行
```

`submit` 登记调用及输入 reservations 后返回 ACK。每个调用的 admission task 独立解析依赖：A 未完成时，依赖 A 的 C 等待；无关 B 仍能被接收、完成 admission 并进入就绪队列。

当前 worker 每次调用原生执行器执行一个 fragment/prompt，等待原生返回后处理下一项。HTTP、heartbeat 和其他 admission 独立于同步计算。当前后端 prompt 计算串行；原生 workflow 内 async/lazy/loop 行为仍由其执行器决定。本地 await 与是否新增 stream API 都不能代替这一设备能力。

显式文件 I/O 使用管理进程的 FileStore 与 HTTP 流，磁盘工作进入 I/O executor，不排进 native 计算队列。停机时仍可用，传输不随计算重启；根来自真实路径配置与受限冷路径 helper。已完成文件独立于 session 持久存在，细节见 [文件 I/O](files.md)。

## 原生适配做什么

- 单节点 eager 请求形成一个明确目标的原生 fragment，普通非 OUTPUT_NODE 也可执行。
- 使用原生输入和 loop 校验、PromptExecutor、动态展开、list mapping 与缓存。
- 预计算对象通过内部 BoundInput/cache-provider 接入，不通过本机文件转发链通信。
- 观察原生 cache set/get，记录节点 outputs 及缓存 entry 版本；工作流借用 view 不阻止原生驱逐。
- 隐藏 PROMPT/DYNPROMPT、rawLink 和 native `extra_data` 保持原生图上下文；完成后不清空原生仍引用的 graph 对象。

这不是另一套 ComfyUI 执行器，也不重写任意 Python 程序。相关 native hooks 与记录的 ComfyUI revision 耦合，升级后需要重新验证。

## 完成与关闭

节点 `output_ready`、整个 invocation 结束、GPU/模型资源回收不是同一个状态。workflow 的声明输出可能早产出，但整体调用仍在原生收尾。

关闭 session 可以先把逻辑等待结算为取消。正在运行的 native 同步任务可能仍在 worker 内，直到原生执行返回；此时 `/queue` 的逻辑列表为空不能证明 worker 或设备已经空闲。控制性回归见 [CPU settlement probe](../../tests/unit/test_runtime_settlement.py)。

输出观察丢失时，原生终态后未产生的节点会结算为 `Skipped`；缓存移除则借用 fetch 报 `Expired`。两者都不通过重算掩盖失效。

## 网络恢复与容量

同一 runtime epoch 内通过原 session 和不透明授权恢复；默认 offline_grace=3600 秒，可显式配置。只有认证客户端流量刷新 seen，worker 活动或僵死 TCP 不刷新。到期取消未开工的依赖/排队工作；已进入原生用户函数的调用自然完成后丢弃资源，不中断其他 session。实际计算重启使旧 refs Expired。

首次提交固定 call ID、规范化请求摘要和签名窗口；同 ID 同内容恢复原调用，不同内容冲突。forget_calls 可释放计算记录，但签名窗口内保留轻量去重墓碑；过期未知请求不执行。容量拒绝新接纳而不静默淘汰可恢复状态。默认 max_sessions=256、max_objects=100000、max_call_records=1000000；对象配额按对象数，不估算任意 Python 对象 bytes。细节见 [所有权](ownership.md) 和 [协议](protocol.md)。

## 扩展边界

新模型节点遵循原生 registry/schema 后，可直接按原名调用并以 opaque refs 传其自定义对象；本地无需导入这些类型或发布节点包装代码。需要完整图上下文的节点应放在 workflow 中。

依赖独有 WebUI、前端 JS、自定义路由或另一个服务进程的插件，需要单独接入；注册了一个 Python 类不等于整个插件生态均已适配。远端部署和新增压力行为以独立验收报告为准。
