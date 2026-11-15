# 远程管理与连接恢复提案

**状态：设计提案，待实施。** 本文收敛 2026-11-15 的只读调研与已确认需求，不代表当前 API 或验收结果。当前实现仍以 [Runtime](runtime.md)、[SDK](sdk.md)、[文件 I/O](files.md) 为准：daemon 启动依赖可用 ComfyUI，files 虽独立于计算队列，仍与 runtime 同进程；没有下述 bash、管理 Operation 或计算 session resume。

目标是在普通 Python 脚本里表达环境限制、插件/模型准备、bash/files 与出图。管理通道需在 ComfyUI 未安装、停止或启动失败时仍可用；管理任务与计算 session/refs 都要应对网络断开，正常脚本由 SDK 自动恢复，无需手写 key、票据或日志检查点。功能完成后统一改写全部 examples；本设计阶段不修改当前示例和冻结产品。

## 进程与数据边界

一个带外管理 daemon 固定绑定一个实例配置，管理一个带内 runtime 子进程；它不是多实例调度器。配置明确记录 `comfy_root`、`runtime_python`、workspace、原生参数及管理工具版本。管理 Python 与 runtime Python 可分离，插件依赖只进入明确选定的目标环境。

| 所在位置 | 职责与持有内容 |
| --- | --- |
| 带外管理 daemon | 生命周期、配置、bash、comfyui/plugins/models、文件 I/O、Operation 表及有界日志；持有 PID、状态与小元数据 |
| 带内 runtime | ComfyUI、NativeAdapter/PromptExecutor、Tensor/模型对象、ObjectStore、epoch、计算 session、Call/Output、graph、`remote.python` |
| 本地 SDK | 同一个 remote 入口，分别管理管理连接与计算连接；本地值、操作观察及同代计算恢复 |

Tensor 不进入管理进程的 Python 对象空间。数据面有两个待实测选项：计算 transport 直连 runtime endpoint，或单公开端口将 HTTP/RPC、二进制 fetch、文件流与 WS 按原始字节流代理到 loopback runtime。后者不能先反序列化或读完整 body 再包装，必须保持 backpressure、media type、压缩协商、heartbeat 与 bounded-wait。代理增加本机网络/复制开销，选择须以部署端口限制和大对象回归测量为依据。

runtime 使用配置的解释器启动 Comani 内存桥接入口；`comfy launch` 启动标准 ComfyUI `main.py`，不能直接代替它。core、插件和共享依赖修改与 runtime 启停受同实例写锁协调；原始 bash 同样进入维护互斥，不能靠分析 shell 文本判断是否只读。运行环境修改前显式停止计算，安装结束后由脚本决定启动。对活动计算的 stop/update 默认拒绝、等待 drain 或显式 force，仍需定最终策略。

## 用户 API 与普通脚本

命名为候选，统一沿用 `remote.comfyui`；不并存 `comfy` 别名。查询返回本地 metadata，变更提供短写和高级 Operation 两种入口：

| 通道 | 候选入口 | 结果 |
| --- | --- | --- |
| `bash` | `run(script, cwd=..., env=..., timeout=..., check=...)`；`begin(...)` | `CommandResult`：退出码、stdout/stderr、时长、截断标记、operation ID |
| `comfyui` | `info/config`、显式 `configure/install/update/start/stop/restart` | 配置、进程与实际版本事实；需重启/部分变更明确返回 |
| `plugins` | `backend/list/versions`、指定包 `install/update/enable/disable/uninstall`；`begin_install(...)` 等 | 逐包源码版本、启用状态、依赖步骤、`restart_required` 与 partial 状态 |
| `models` | `categories/list/resolve`、`download(...)`；`begin_download(...)` | 来源候选及明确文件；完成后返回 `RemoteFile` |

`.bash.run` 是普通 async 方法：await 或调度 coroutine 时才提交，不采用 `remote.node/python` 调用即 eager 提交的语义。不接收 RemoteRef/Future，不返回 Call/Output。与 files 一样，graph scope 内 await 会立即执行真实 I/O，不被捕获；重放 graph 不重跑这些准备动作。

每次 bash 使用独立非交互 `bash --noprofile --norc -c`，首版仅 Linux/bash、无 PTY/stdin。cwd 默认实例 workspace，`cd/export` 不跨调用持久化；env 只覆盖该子进程，不自动继承管理凭证。shell 中 `python` 由 PATH 决定，进入 Comfy 目录不等于激活 venv。stdout/stderr 并发读取、分别限额并标截断，不承诺跨流全序。`check` 默认值待定；非零、操作超时和显式取消均须可区分。取消/超时按进程组 TERM→限时 KILL→reap，避免留下 pip/git 子进程。

下例全部为未来 API 示意，规格和环境是调用方明确选择的普通 Python 值；不是当前可运行示例。为避免预先决定离线 roots 方案，先启动 runtime 发现真实模型根，再做 create-only 模型准备。

```python
async with Client(URL, token=TOKEN) as remote:
    info = await remote.comfyui.info()
    assert info.runtime_python == RUNTIME_PYTHON
    assert info.comfy_root == COMFY_ROOT
    await remote.comfyui.stop(wait=True)
    await remote.plugins.install(PLUGIN_SPECS)  # 失败即停，不继续出图
    await remote.bash.run(PIP_CHECK_COMMAND, cwd=COMFY_ROOT, check=True)
    await remote.comfyui.start()

    roots = await remote.files.roots(category="checkpoints")
    source = await remote.models.resolve(MODEL_URL)
    artifact = select_requested_file(source, EXPECTED_FILE_ID)
    model_file = await remote.models.download(artifact, root=roots[0], path=MODEL_NAME)
    staged = await remote.files.upload("reference.png")

    checkpoint = remote.node("CheckpointLoaderSimple", ckpt_name=model_file.native_name)
    image = remote.node("LoadImage", image=staged.native_name)
    positive = remote.node("CLIPTextEncode", text=PROMPT, clip=checkpoint.output(1))
    negative = remote.node("CLIPTextEncode", text="", clip=checkpoint.output(1))
    sized = remote.node("ImageScale", image=image.output(0), width=512, height=512,
                        upscale_method="lanczos", crop="center")
    latent = remote.node("VAEEncode", pixels=sized.output(0), vae=checkpoint.output(2))
    sampled = remote.node("KSampler", model=checkpoint.output(0),
        positive=positive.output(0), negative=negative.output(0),
        latent_image=latent.output(0), seed=42, steps=20, cfg=7.0,
        sampler_name="euler", scheduler="normal", denoise=0.7)
    decoded = remote.node("VAEDecode", samples=sampled.output(0), vae=checkpoint.output(2))
    await remote.node("SaveImage", images=decoded.output(0), filename_prefix="managed_run")
    await remote.files.remove(staged)
```

`select_requested_file` 是脚本自己的普通选择函数，不是 SDK DSL；多候选不能暗取第一个。实际 examples 还需完整失败收尾、引用释放和结果取回。整段脚本不是可回放事务，SDK 恢复原操作/计算结果，不因断线重跑 SaveImage、Python 副作用或整段脚本。

## 管理 Operation：执行与观察分开

普通 `await bash.run/plugins.install/models.download` 内部使用同一个 Operation 机制并等待本地结果。高级入口 `begin_*` 返回管理 `Operation`，提供 `status()`、`result()`、`logs(after=cursor, follow=True)` 和显式 `cancel()`；它没有输出槽、图引用、Comfy cache 或 `.fetch()`。跨新 Client 附着才使用 `export_handle()` / `operations.get(handle=...)` 等高级入口，具体命名待定；常规脚本不手工维护这些信息。

| 事件 | 管理任务 | 计算会话 |
| --- | --- | --- |
| 网络断线、代理超时 | 已接纳任务继续；SDK 重连查询同操作 | 在有界离线宽限内保留原 session/call/对象，恢复原等待与取回 |
| 本地取消 await | 停止观察，不隐式取消远端任务 | 不因此重提或取消底层计算，沿用显式计算契约 |
| `Client.close()` | 关本地连接，不取消已接纳管理任务 | 仍明确撤销计算 session；离线未获确认须说明可能保留至宽限到期 |
| `op.cancel()` / 操作自身 timeout | 停属于该操作的进程/网络，读回已完成或 partial 事实 | 不等同 HTTP 等待 timeout |
| runtime 真停止/重启 | 管理连接和下载/shell 继续 | 新 epoch；旧 refs 为 Expired，未完成 Call/Output 明确终态，不自动重放 |
| 管理 daemon 崩溃/重启 | 本轮不保证恢复，旧未知结果明确报错 | 不承诺旧计算自动跨代恢复 |

首版用 daemon 内有界 Operation 表与日志 spool，不建立历史数据库。最小状态覆盖 accepted/queued/running/succeeded/failed/cancelling/cancelled。取消安装不代表回滚；实际 clone、pip 或脚本部分变更保留在结果中。日志 cursor 单调前进，过期明确返回可用起点或 `LogExpired`，不能静默跳过。活动任务不因结果缓存淘汰而消失，并发、队列、结果、日志及 staging 磁盘都有容量边界。

提交响应丢失由 SDK 内部有界去重处理：首次发送前固定 key、原 daemon epoch、服务器签发且限制有效期的 window/ticket 和规范化请求。daemon 在启动外部动作前登记身份/实例/key、请求摘要与 operation ID；同 key 同摘要返回原操作，不同摘要报冲突。SDK 在原窗口内查询或以原身份重送，绝不生成新 key 补跑。去重映射至少覆盖原窗口，容量不足拒绝新接纳；窗口外或 epoch 变化后的未知结果返回 Unknown/Expired，不自动执行。任务运行可长于提交窗口，已知 ID 仍按结果保留规则查询。`latest/main` 首次接纳后固定实际版本，重连不重新解析为更新版本。此机制不承诺跨 daemon crash 的 ExactlyOnce。

## 计算 session 与对象的网络恢复

计算保留现有 Call/ObjectStore，另加同代 resume，不转成管理 Operation。握手需给出可恢复的 session 授权与 runtime epoch，resume 验证同管理身份、原授权、epoch 及宽限。暂时传输错误令 SDK 进入 reconnecting，重建连接后继续原 `wait_result/fetch`，不能先将 future 终结或调用总是新建 session 的旧握手路径。

SDK 在首次发送前固定 `call_id` 与已编码 spec；接纳响应丢失后查询或重送同 ID，daemon 核对请求摘要。现有 `forget_calls` 可移除调用记录，须另保留覆盖恢复窗口的去重信息，避免迟到 submit 再执行副作用。输入持有到接纳事实明确；离线 release/forget 排队，resume 后以原 epoch 幂等补发。

宽限从最后已认证客户端接触起算，不能被内部 worker 活动或僵死 TCP 无限续长。到期按明确策略收尾，配额不足明确失败。owned 引用在宽限内保留；borrowed 仍可能被 Comfy 缓存驱逐或因版本失效，网络恢复不会将其变为 owned。真实 runtime 重启始终失效，重新启动后显式建立新计算代际；纯图是否可重新提交另验，含旧 refs 的图不能跨代复用。

待定的是宽限时长、活动/离线 session 与对象配额、结果保留，以及宽限到期时运行中 call 的取消/完成后清理策略。用户已选择需要计算网络恢复并接受宽限期资源占用，不再把“是否支持恢复”当待选项。

## 复用后端与模型发布

管理后端采用版本固定的 CLI/provider 子进程适配，避免把使用全局 cwd/env/单例的 command 模块 import 到管理 daemon。目标解释器必须读回核对，不能只靠 `--workspace`：固定调研的 comfy-cli 会依次检查 VIRTUAL_ENV、CONDA_PREFIX、workspace venv，最后可能回退到工具解释器。[解释器选择源码](https://github.com/Comfy-Org/comfy-cli/blob/4f554bb5a9e5d0b5d04146e7bdf8cac2ceb729a5/comfy_cli/resolve_python.py#L27)

新实例建议固定 comfy-cli `4f554bb5…` 与目标 ComfyUI pin 的 packaged Manager **4.2.2**。选择本次调研 release **4.3** 或 `manager-v4` commit `1c3a47b6…` 必须显式记录并单独集成验证；legacy `main` 的 `cm-cli.py` 不是新版所需的 `cm_cli` 包。现有 legacy 实例保留原布局、直接适配，不在连接/启动时迁移。[ComfyUI 固定 pin](https://github.com/Comfy-Org/ComfyUI/blob/eee59185ff4f43ae2d9ad122b245619d27d9daad/manager_requirements.txt#L1)、[CLI 检测](https://github.com/Comfy-Org/comfy-cli/blob/4f554bb5a9e5d0b5d04146e7bdf8cac2ceb729a5/comfy_cli/command/custom_nodes/cm_cli_util.py#L28)、[Manager 4.2.2](https://pypi.org/project/comfyui-manager/4.2.2/)、[4.3](https://pypi.org/project/comfyui-manager/4.3/)

选定版本后必须验证机器输出和部分失败：退出 0 不直接等于业务成功，逐包读回版本/启用状态、core HEAD 与依赖步骤；`pip check` 也不能证明所有 CUDA/ABI 健康。调研的 v4 `--no-deps` 仍可执行 `install.py`，`update all` 可更新 Comfy core；plugins 只接明确包 ID，不把 `all/comfy/comfyui` 传到底层。磁盘已改与 live registry 已生效分列，后者需重启核对；4.3 源码证据不能冒充 4.2.2 已通过运行验收。[安装路径](https://github.com/Comfy-Org/ComfyUI-Manager/blob/1c3a47b6cef16208c6eb7b9fc76c16775f6087a2/comfyui_manager/legacy/manager_core.py#L767)、[update all](https://github.com/Comfy-Org/ComfyUI-Manager/blob/1c3a47b6cef16208c6eb7b9fc76c16775f6087a2/cm_cli/__main__.py#L833)

模型只新增来源解析与外网→daemon 的获取，不引入模型对象/显存 cache。HF 优先复用官方 hub 的 revision/cache/partial 能力；Civitai 与直链使用薄 resolver，明确版本和具体文件。旧项目只提取链接语法、归一化规格与用例，不搬旧下载器、自动系统装包、仅凭 size 跳过或未经核对的 Range append。comfy-cli 下载可作候选，但须阻止缺包自动 pip、半文件落最终路径及覆盖默认；后端选择仍需验证。[Hub 下载文档](https://huggingface.co/docs/huggingface_hub/guides/download)、[固定 CLI 模型实现](https://github.com/Comfy-Org/comfy-cli/blob/4f554bb5a9e5d0b5d04146e7bdf8cac2ceb729a5/comfy_cli/command/models/models.py#L798)

发布流程复用 files 的原生 category/多根优先级、路径边界与 create-only：resolve 明确 artifact→显式选 root/path→隔离 staging→核对响应/大小/SHA→发布前再查 root generation 与同名 shadow→原子发布→返回 `RemoteFile` 和来源验证依据。有上游 hash 必须匹配；只有本地 digest 就标未做上游摘要验证。来源 DTO 和日志不带凭证或 signed URL。相同 hash 复用也须显式选择并实际校验。添加文件与刷新文件名 cache 不表示已载入模型，删除文件不自动卸载正在用的对象。

SDK↔daemon 断线只中断观察；provider↔daemon 断流才涉及字节续传。后者绑定不可变 artifact/revision、长度/hash 与 validator；Range 必须核对 `206/Content-Range`，`200` 安全重下，`416` 重新验证，不能盲 append 或直接成功。来源变更或无法证明续传安全时，同 Operation 明确重下，重试有上限。客户端本地 files upload/download 仍按当前失败清理、无 resume 契约；若增加续传，需另设计 transfer ID、源 revision、offset 确认、part 保留/配额/过期与完整校验，后台 Operation 本身不能补齐缺失字节。

roots 在线时从真实 `folder_paths` 导出顺序与配置 generation；离线已观测值标 last-observed，配置变化需阻止发布或重新选择，native 可见性为 unknown。首次冷启动尚无可信 roots 时有两种选择：先启动 runtime 发现，或增加只加载原生路径配置的独立 helper；helper 不 import 插件，不能发现插件运行时注册的 category。HF snapshot 也待选：首版仅具体单文件，或明确 manifest/allow/ignore/commit，以及只返回逐文件结果还是承诺整组可用；多文件集不是一个 RemoteFile。

## 实施顺序与验收门槛

1. 建立独立管理 daemon、单 runtime 生命周期与授权边界；实现共同 Operation、接纳去重和有界日志，再接 bash。
2. 实现计算同 session resume、提交摘要/去重保留、离线 release/close/宽限收尾；保持 owned/borrowed 与 epoch 契约。
3. 接版本固定的 comfyui/plugins 后端和模型 resolve/download；读回实际结果，补 provider 续传与原子发布验证。
4. 对直连/原始流代理测大 Tensor、files、heartbeat 和长等待回归，决定数据面；全部 examples 迁入同一普通 Python 准备→计算流程。

必须覆盖：接纳回复丢失与重复并发只执行一次；同 key 改参数冲突、窗口过期/daemon 换代不补跑；断线及 Client.close 后管理任务继续、显式 cancel 清理进程组；计算同代恢复、SaveImage/Python 不重复、GC 消息补发与宽限到期释放；runtime 真重启旧 refs 失效；安装部分失败不继续出图；provider `206/200/416`、来源变更/hash 不符不发布；当前 files 断线仍失败清理。真实测试前均只列为验收计划。

待收敛的少数边界：计算宽限/配额/到期 call 策略；去重窗口与结果/日志保留容量；活动计算遇维护的策略；直连或原始流代理；离线首次 roots helper；HF 单文件或 snapshot 的发布契约。首版不扩展为跨 daemon crash 的 ExactlyOnce、历史数据库、通用任务调度或新 DSL。
