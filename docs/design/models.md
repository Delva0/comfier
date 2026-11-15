# 模型来源、获取与原生发布

实现：[SDK](../../src/comani/management.py)、[来源解析](../../src/comani/daemon/management/providers.py)、[模型服务](../../src/comani/daemon/management/models.py)、[transport](../../src/comani/daemon/management/transports.py)。

models 管理原生磁盘文件，不创建模型对象或显存 cache。来源解析、版本查询和下载全部在 daemon；SDK 只传参数和返回本地 DTO。模型加载仍由原生 Loader 节点执行，删除文件不会自动卸载已加载对象。

## 选择具体 artifact

```python
source = await remote.models.resolve({
    "repo_id": "owner/model-repo",
    "revision": REQUESTED_REVISION,
    "path": "weights/model.safetensors",
})
matches = [file for file in source.files if file.source.get("path") == "weights/model.safetensors"]
if len(matches) != 1:
    raise ValueError("Select exactly one requested file")
artifact = matches[0]
roots = await remote.files.roots(category="diffusion_models")
selected = [root for root in roots if root.path == SERVER_MODEL_ROOT]
if len(selected) != 1:
    raise ValueError("Choose one actual remote root")
file = await remote.models.download(
    artifact, root=selected[0], path="model.safetensors", reuse=True,
    transport="auto", connections=8,
)
model = remote.node("UNETLoader", unet_name=file.native_name, weight_dtype="default")
```

示例的 repo、revision、server root 由调用方指定，不是客户端文件路径。HF 返回的实际 commit 固定在 ModelSource/ModelArtifact，文件包括 size、SHA256 或 Git blob hash 等可得证据。resolve 接受 HF URL 或 repo_id/revision/path Mapping；Civitai 接受模型/版本 URL 或 model_id/version_id/file_id。普通 HTTP(S) URL 也可解析为单文件来源。

Civitai 的版本可能同时有不同精度、格式和 pruned 文件，必须用 file_id 或自己的明确选择条件，不自动取第一个。其 sizeKB 是近似值，不作为精确 byte 长度证明；发布校验使用可信摘要与响应长度。`versions(source)`、`categories()` 和 `list(category=..., root=..., prefix=...)` 都是远端查询；list 返回 files wire metadata 与 truncated 标记，普通 stat 默认不扫描大模型。

直链的 signed URL/凭证保留在 daemon 的有界来源注册表，DTO/日志只返回净化 URL 与不透明 source_id。过期来源需重新 resolve 后才可开启新下载；已接纳操作持有自己的来源。HF_TOKEN、CIVITAI_TOKEN 配置在 daemon 环境，SDK 不打印或传回它们；授权失败明确报告。

## 分层职责与 transport

1. Provider 解析 repo/version/file，固定不可变 revision。
2. 脚本选择确切 artifact 与原生 root/path。
3. transport 获取到隔离 staging。
4. 核对身份、长度、上游摘要/Git blob hash。
5. 重查 roots generation、同名 shadow 和 create-only 目标，原子发布。

`download(..., transport="auto"|"http"|"aria2"|"hf", connections=8)` 显式控制引擎。auto 对 HF 在目标解释器已装官方 hub 时优先 hub/Xet；有 SHA 且已有 aria2c 时可用 aria2 多连接，否则使用 HTTP。强制 hf/aria2 缺工具明确失败，不自动 pip 或 apt。官方 hub 在配置的目标解释器运行，复用该账号/缓存机制；现有 aria2c 二进制复用。

引擎错误允许同一个不可变 artifact 安全从零 fallback；revision/hash 校验失败不能通过换引擎掩盖。支持官方 hub/Xet 路由不等于 private/gated、账号权限和大 LFS 已做真实远端验收；新报告应分别说明 public metadata、小文件、LFS/Xet 和受限仓库覆盖。

Provider→daemon 断流与 SDK→daemon 断流不同：SDK 恢复观察同一个 Operation；provider 的字节恢复必须绑定原 artifact/revision、validators、长度/hash。HTTP 核对 206 与 Content-Range，200 从零重下，416 重新校验；不盲 append。来源变化或无法证明安全时明确失败或在同操作从零重新获取，重试有界。

有上游 SHA 必须匹配；只有本地 digest 时不声称经过上游摘要认证。`reuse=True` 也实际核对现有文件内容/身份，不仅凭名字或 size 跳过。发布采用 files 的路径、root、shadow 与 create-only 规则，并使 native filename-list cache 失效；不表示模型已经加载。

## HF manifest 与 snapshot

```python
manifest = await remote.models.manifest(
    {"repo_id": "owner/model-repo"}, revision=REQUESTED_REVISION,
    allow=["weights/*.safetensors", "*.json"], ignore=["weights/experimental/*"],
)
snapshot = await remote.models.snapshot(
    manifest, root=selected_root, path="my-pinned-model",
    publish="directory", transport="auto",
    max_total_bytes=EXPLICIT_OPTIONAL_STAGING_LIMIT,
)
```

manifest 在 daemon 枚举并固定同一个 HF commit。allow/ignore 为仓库路径模式；allow=None 选择完整仓库，也可以只选择明确的子集。ModelManifest 包含固定 revision、所有 ModelArtifact 与已知总大小；不是单个 RemoteFile。

snapshot 的 **publish 必填且无默认值**：

| 选择 | 发布和失败语义 |
| --- | --- |
| directory | 构建全新的完整目录，验证全部文件后同文件系统原子发布；目标目录必须不存在，失败不暴露半个新目录 |
| files | 文件逐个 create-only 发布，原生逐项可见；后续失败可保留前面成功文件，结果/OperationError 明确 partial_changes |

directory 返回目录路径、root、完整 files/items 与 native_visibility；它不是跨任意现有目录的替换或文件系统事务。files 模式适用于明确接受逐项可见的场景，不能宣称整组原子。ModelSnapshot 的 complete/partial_changes 表达实际结果。

H3 的 UNET、text encoder、VAE 可能来自不同 provider 和不同 roots，不能把它们假称 HF 目录原子事务。用普通 asyncio.gather 下载，全部成功作为出图前就绪屏障；失败自然中断脚本，已完成文件仍持久存在。[H3 主脚本](../../examples/minimax_h3_remote.py) 正是这一流程。

## 磁盘与限额

默认 `max_file_bytes=None` 不添加任意单文件上限；实例可显式配置单文件 policy。snapshot 的 `max_total_bytes=None` 是独立的可选总 staging 限额，不把单文件限额当总额，也没有隐藏的 64 GiB 上限。已知大小先检查实际可用磁盘；途中 ENOSPC 报 DiskFull。上游缓存、staging、模型最终目录及跨文件系统复制仍可能同时占磁盘，成功不能由供应商磁盘报价推定。

根目录来自实际 folder_paths。运行时观测 live；停机用 last-observed；冷启动可由不导入 Torch/插件的路径 helper 发现原生路径配置。插件运行时注册的未知 category 不能凭空构造。配置变化需要重新发现，发布前检查 generation；稳定未变的 root tuple 保持稳定 ID。详细边界见 [文件 I/O](files.md)。
