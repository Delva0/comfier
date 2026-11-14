# Driver 与原生 runtime 边界

实现：[service.py](../../src/comani/_runtime/service.py)、[adapter.py](../../src/comani/_runtime/adapter.py)、[入口](../../src/comani/_runtime/__main__.py)。

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
