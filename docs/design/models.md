# 模型来源、获取与原生发布

实现：[SDK](../../src/comani/management.py)、[本地查询](../../src/comani/_model_providers.py)、[下载来源](../../src/comani/daemon/management/providers.py)、[模型服务](../../src/comani/daemon/management/models.py)、[transport](../../src/comani/daemon/management/transports.py)。

models 管理原生磁盘文件，不创建模型对象或显存 cache。`comani.models.resolve/versions/manifest` 在客户端查询 HF/Civitai 并返回可交给所选 daemon 的来源值，无需 Client；`remote.models` 没有这些纯查询。categories/list、下载、snapshot 和删除依赖远端实际状态，使用模块当前 Client 或显式 remote.models。模型加载仍由原生 Loader 节点执行，删除文件不会自动卸载已加载对象。

## 选择具体 artifact

```python
source = await comani.models.resolve({
    "repo_id": "owner/model-repo",
    "revision": REQUESTED_REVISION,
    "path": "weights/model.safetensors",
})
matches = [file for file in source.files if file.source.get("path") == "weights/model.safetensors"]
if len(matches) != 1:
    raise ValueError("Select exactly one requested file")
artifact = matches[0]
file = await remote.models.download(
    artifact, category="diffusion_models", path="model.safetensors", reuse=True,
    transport="auto", connections=8,
)
model = remote.node("UNETLoader", unet_name=file.native_name, weight_dtype="default")
```

示例的 repo、revision、原生 category 和相对 path 由调用方指定，不是客户端文件路径。HF 返回的实际 commit 固定在 ModelSource/ModelArtifact，文件包括 size、SHA256 或 Git blob hash 等可得证据。resolve 接受 HF URL 或 repo_id/revision/path Mapping；Civitai 接受模型/版本 URL 或 model_id/version_id/file_id。普通 HTTP(S) URL 的下载推荐直接传给远端 models.download，无需 provider metadata 查询。

`download` 和 `begin_download` 必须在 `root=` 与 `category=` 中明确选择一个。`category` 是实际 ComfyUI 模型类别，由调用者按 Loader 的要求提供；不会从 provider、文件名或内容猜测类型。daemon 使用既有原生目录快照和查找顺序，支持原生别名（如 `unet` → `diffusion_models`、`clip` → `text_encoders`）。目标相对路径已有文件时选择原生查找首先可见的文件；只有显式 `reuse=True` 且 SHA256 一致才复用。前一目录的同名文件摘要不符会返回 `ChecksumMismatch`，不会跳过它到后面的目录寻找相同摘要。没有现有文件时发布到该类别第一个原生根，仍遵循路径、shadow 和 create-only 检查。

需要明确写入某个原生目录时，仍先用 `remote.files.roots(category=...)` 选出实际 `FileRoot`，然后传 `root=selected_root`；其原有语义和安全检查不变，不能同时传 `category`。返回的 `RemoteFile.root` 包含实际目录、规范类别和原生优先级。类别未知或没有配置目录时明确失败，不创建猜测目录；单文件类别选择不扩展到 `snapshot` 或本地文件上传。

类别选择不会为发现目录而启动 worker，也不会遍历所有节点的 `INPUT_TYPES`。目录来自运行中 live、停止后 last-observed 的快照；冷实例仍由既有路径 helper 读取原生路径配置。只在插件运行时注册、尚未观测到的类别仍不可用，应先按该插件的正常准备步骤注册/观测后再下载。现有显式 root 的带外下载能力不依赖运行中的 worker。

Civitai 的版本可能同时有不同精度、格式和 pruned 文件，必须用 file_id 或自己的明确选择条件，不自动取第一个。其 sizeKB 是近似值，不作为精确 byte 长度证明；发布校验使用可信摘要与响应长度。`comani.models.versions(source)` 是本地 provider 查询；categories() 和 list(category=..., root=..., prefix=...) 才查询远端。list 返回 files wire metadata 与 truncated 标记，普通 stat 默认不扫描大模型。

本地 provider 查询使用客户端进程的 `HF_TOKEN`、`CIVITAI_TOKEN`（兼容别名 `CIVITAI_API_TOKEN`），也可显式传 `token=...`；显式参数优先，两个 Civitai 环境变量同时存在时 `CIVITAI_TOKEN` 优先。返回值不携带 provider token。真正下载使用 daemon 的网络及其独立配置的凭证，本地查询成功不保证远端可下载；授权失败分别报告。需要受限模型时，应明确将该 provider 凭据配置给目标 daemon；客户端全部凭据不会自动透传。aria2 不接收 provider Bearer header：先按原 origin 安全解析重定向，能取得匿名或签名下载 URL 时直接交给 aria2；仍需 Bearer 的源才使用本次 Operation 的临时 loopback Range relay，凭据只发给原 origin，跨 origin 重定向不携带。relay 流式转发且连接数有界，Operation 结束即关闭；匿名下载不经过 relay。

普通或 signed HTTP(S) URL 可直接交给 `models.download(url, root=..., path=...)`；daemon 在接纳下载时管理实际来源，公开结果/日志净化秘密 query。不要把本地生成的 source_id 当成远端来源。signed URL 仍受上游有效期限制，不能从净化后的 URL 恢复秘密参数。

## 分层职责与 transport

1. Provider 解析 repo/version/file，固定不可变 revision。
2. 脚本选择确切 artifact、原生 category/path 或显式 root/path；daemon 按原生规则确定目录。
3. transport 获取到隔离 staging。
4. 核对身份、长度、上游摘要/Git blob hash。
5. 重验所选 root 的配置与目录身份、同名 shadow 和 create-only 目标，原子发布。

`download(..., transport="auto"|"http"|"aria2"|"hf", connections=8)` 控制引擎。默认 `auto` 使用 aria2。daemon 部署启动会预先准备下载依赖；每次需要默认或显式 aria2 传输时仍独立检查实际二进制，缺失时通过宿主的 APT/APK/DNF/YUM 自动安装，再执行 `aria2c --version` 验证，不依赖“曾部署成功”的标记。完整 SHA 命中的 `reuse=True` 不发生传输，也不为补装下载器打断离线复用。同 daemon 的并发准备共用一次安装尝试；不同 daemon 进程仍依赖包管理器原生锁。root 可直接安装，非 root 仅尝试非交互 sudo；没有合法权限或安装失败时，`DownloadToolInstallationFailed` 返回实际阶段与原因，不切换为 HTTP。安装过程使用既有 Operation/进程组清理；部署失败不关闭带外管理端。已有的显式 `http`、`hf` 是调用者主动覆盖；官方 hub 在配置的目标解释器运行。aria2 使用受控参数，不加载用户 aria2 配置或 netrc；默认 8 个分段连接，允许 1–16，最小分段 1 MiB，实际连接数仍受文件大小和源的 Range 支持限制。

Operation 的 progress 返回请求与实际引擎（`requested_transport`、`transport`），以及 aria2 的 `source_route`、实际连接配置、完整性策略和尝试次数。引擎缺失或失败保留该引擎的明确错误，不静默换用另一实现；revision/hash 校验失败也不会通过换引擎掩盖。SHA256 或 Git blob SHA1 仍在发布前完整校验。无预期 digest 的 URL 也可下载：强 ETag 使用 `If-Match` 钉住分段版本；没有强 validator 时限单连接，失败后清空 partial 并重新探测、从零有界重试，避免混拼不同响应。

本轮断点恢复只覆盖同一 Operation 中 aria2 的有界重试。失败、取消或正常关闭会清除临时 partial 与 `.aria2` 控制文件；后续 Operation、daemon 重启或代码重部署不承诺复用未完成下载。已完成模型的跨运行复用依赖持久原生模型目录、相同目标路径与 `reuse=True` 的完整 SHA 检查，不依赖这个临时目录。支持官方 hub/Xet 路由不等于 private/gated、账号权限和大 LFS 已做真实远端验收；新报告应分别说明覆盖范围，受控每连接限速测试的收益不代表互联网吞吐承诺。

Provider→daemon 断流与 SDK→daemon 断流不同：SDK 恢复观察同一个 Operation；provider 的字节恢复必须绑定原 artifact/revision、validators、长度/hash。HTTP 核对 206 与 Content-Range，200 从零重下，416 重新校验；不盲 append。来源变化或无法证明安全时明确失败或在同操作从零重新获取，重试有界。

有上游 SHA 必须匹配；只有本地 digest 时不声称经过上游摘要认证。`reuse=True` 也实际核对现有文件内容/身份，不仅凭名字或 size 跳过。发布采用 files 的路径、root、shadow 与 create-only 规则，并使 native filename-list cache 失效；不表示模型已经加载。

单文件下载和 `snapshot(publish="files")` 的获取结果在私有、daemon 所有且独占的 staging 中完成内容校验。同文件系统发布直接以原子 create-only hard link 采用该 inode，再移除 stage 名字，不再复制整份 payload。只有实际 `EXDEV` 才回退到有界复制，先检查目标空间，并独立核对复制后的完整 SHA256；其他 link 错误不通过复制绕过。取消会等待活动磁盘工作收尾；若已经越过发布点，结果保留已发布文件的 metadata。

## HF manifest 与 snapshot

```python
manifest = await comani.models.manifest(
    {"repo_id": "owner/model-repo"}, revision=REQUESTED_REVISION,
    allow=["weights/*.safetensors", "*.json"], ignore=["weights/experimental/*"],
)
snapshot = await remote.models.snapshot(
    manifest, root=selected_root, path="my-pinned-model",
    publish="directory", transport="auto",
    max_total_bytes=EXPLICIT_OPTIONAL_STAGING_LIMIT,
)
```

manifest 在客户端枚举并固定同一个 HF commit，无需 Client；snapshot 在远端获取并发布。allow/ignore 为仓库路径模式；allow=None 选择完整仓库，也可以只选择明确的子集。ModelManifest 包含固定 revision、所有 ModelArtifact 与已知总大小；不是单个 RemoteFile。

snapshot 的 **publish 必填且无默认值**：

| 选择 | 发布和失败语义 |
| --- | --- |
| directory | 构建全新的完整目录，验证全部文件后同文件系统原子发布；目标目录必须不存在，失败不暴露半个新目录 |
| files | 文件逐个 create-only 发布，原生逐项可见；后续失败可保留前面成功文件，结果/OperationError 明确 partial_changes |

directory 返回目录路径、root、完整 files/items 与 native_visibility；它不是跨任意现有目录的替换或文件系统事务。files 模式适用于明确接受逐项可见的场景，不能宣称整组原子。ModelSnapshot 的 complete/partial_changes 表达实际结果。

H3 的 UNET、text encoder、VAE 可能来自不同 provider 和不同 roots，不能把它们假称 HF 目录原子事务。用普通 asyncio.gather 下载，全部成功作为出图前就绪屏障；失败自然中断脚本，已完成文件仍持久存在。[H3 主脚本](../../examples/minimax_h3_remote.py) 正是这一流程。

## 磁盘与限额

默认 `max_file_bytes=None` 不添加任意单文件上限；实例可显式配置单文件 policy。snapshot 的 `max_total_bytes=None` 是独立的可选总 staging 限额，不把单文件限额当总额，也没有隐藏的 64 GiB 上限。已知大小先检查实际可用磁盘；途中 ENOSPC 报 DiskFull。同文件系统的上述发布不再分配第二份 payload；跨文件系统仍需完整目标空间。HF Hub 等引擎自身的 acquisition cache 与 payload 峰值没有因此消失，成功不能由供应商磁盘报价推定。

stage 身份、revision、独占 link 和私有目录检查遵循 [文件 I/O](files.md) 的既有权限边界，不隔离具有 daemon 同等文件权限的恶意 writer。revision 不是内容快照；不能据此声称拒绝同 UID 的所有同长度改写。实际低空间、普通磁盘大模型及保留反例的验收归属见 [管理验收](../management_acceptance.md)。

根目录来自实际 folder_paths。运行时观测 live；停机用 last-observed；冷启动可由不导入 Torch/插件的路径 helper 发现原生路径配置。插件运行时注册的未知 category 不能凭空构造。配置变化需要重新发现。支持 no-follow 目录句柄的平台会在获取前保存所选 root 的设备、inode、owner/group 与 mode，发布前刷新原生配置并在发布锁内复查；其他 category 新增或同类 root 优先级变化不会使未变的目标目录失效，也不会重定向到另一目录。无法保持目录句柄的平台仍保守核对完整 generation。已观察到移除的 root ID 会退役，重新注册同一路径会产生新 ID；未变目录的 ID 保持稳定。目录内容与 mtime/ctime 不属于目录身份，现有 SHA、stage 独占、路径/link、shadow、parent 和 create-only 检查继续生效。这不是对同权限恶意 writer 的文件系统隔离。详细边界见 [文件 I/O](files.md)。
