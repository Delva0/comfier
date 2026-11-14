# Driver 与原生 runtime 边界

实现：[CLI](../../src/comani/__main__.py)、[runtime 入口](../../src/comani/_runtime/__main__.py)、[service.py](../../src/comani/_runtime/service.py)、[adapter.py](../../src/comani/_runtime/adapter.py)。

## 启动边界

当前 `comani daemon` 与 `python -m comani daemon` 把参数交给同一解释器中的 runtime。部署者先准备可用的 ComfyUI 环境，`--comfy-root` 再选择其源码来源；这个启动入口不创建环境、安装依赖、选择版本或托管机器。原有 `python -m comani._runtime` 也保留。

原生计算参数通过 `--` 传入，在原生模块初始化前应用。这个入口运行 headless backend，没有再启动第二个 ComfyUI HTTP/WebUI 服务；原生 parser 的语法覆盖范围不等于 driver 的实现范围。不支持的服务或 cache 模式必须明确拒绝，不能静默接受后忽略。

### 原生参数的实际边界

设备环境在 Torch 首次导入前设置；精度、显存 reserve、smart-memory 等计算策略交给所选 backend。CLASSIC、LRU、RAM_PRESSURE cache 及 RAM 阈值遵循原生设置，`--high-ram` 沿用原生别名语义。base/models 路径在 `folder_paths` 首次导入时应用；额外模型 YAML、input/output/user 路径、preview、自定义节点治理、prestartup 与 backend 节点初始化复用原生初始化流程。指定 temp 目录时，实际临时路径按原生规则为 `<指定目录>/temp`。

API/partner 后端节点不会被 driver 永久禁用；`--offline`、`--disable-api-nodes` 沿用所选 backend 的节点治理。节点依赖和所需执行 metadata 仍由调用方提供，见 [SDK metadata](sdk.md#可选执行-metadata)。这不增加 WebUI、前端 JS 或第三方 HTTP route 的兼容承诺。

当前保留 `NoAssets` headless 边界。下列 **`--` 后的原生选项** 被明确拒绝，作为参数错误在 Torch 初始化前结束；Comani 自己的 `--host/--port` 仍可用：

| 不提供的服务/行为 | 拒绝的原生选项 |
|---|---|
| 第二个原生 HTTP/TLS/CORS 服务 | `--listen`、`--port`、`--tls-keyfile`、`--tls-certfile`、`--enable-cors-header`、`--max-upload-size`、`--enable-compress-response-body` |
| 原生前端与浏览器启动 | `--auto-launch`、`--disable-auto-launch`、`--front-end-version`、`--front-end-root`、`--multi-user`、`--dont-print-server` |
| Manager、agent、asset/database 服务 | `--enable-manager`、`--enable-manager-legacy-ui`、`--disable-manager-ui`、`--enable-agent`、`--enable-assets`、`--enable-asset-hashing`、`--database-url` |
| 独立测试/打包/挂起模式 | `--quick-test-for-ci`、`--windows-standalone-build`、`--debug-hang` |

另行拒绝 `--cache-none`：当前对象绑定需要 cache provider，原生 NullCache 不提供它。以上不是覆盖所有原生参数的 allowlist，也不承诺未来 ComfyUI 新参数自动兼容；更换 backend 后需要验证对应选项及节点。

## 一个设备入口

一个 daemon 进程包含 HTTP 事件循环和一个 native worker 线程，后者加载并持有一份 ComfyUI runtime。daemon 不是连接另一个已运行 ComfyUI 进程后直接取得其 Python 对象。

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

## 扩展边界

新模型节点遵循原生 registry/schema 后，可直接按原名调用并以 opaque refs 传其自定义对象；本地无需导入这些类型或发布节点包装代码。需要完整图上下文的节点应放在 workflow 中。

依赖独有 WebUI、前端 JS、自定义路由或另一个服务进程的插件，需要单独接入；注册了一个 Python 类不等于整个插件生态均已适配。远端部署和新增压力行为以独立验收报告为准。
