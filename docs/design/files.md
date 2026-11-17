# 原生文件与模型 I/O

实现：[客户端文件代理](../../src/comfier/files.py)、[文件存储](../../src/comfier/daemon/files.py)、[HTTP 流](../../src/comfier/daemon/file_http.py)。管理 daemon 独立持有文件存储，计算未启动或重启时仍可用。文件操作是显式 async I/O，不经过 native 计算队列，也不改变 Call/Output 的语义。模块 `cf.files` 使用当前显式 Client 上下文；`remote.files` 始终使用指定 Client。

## 上传后交给原生节点

```python
import comfier as cf

async with cf.Client(url, token=token) as remote:
    staged = await remote.files.upload("local.png")
    image = mask = None
    try:
        image, mask = await remote.node("LoadImage", image=staged.native_name)
        local_image = await image.fetch()
    finally:
        for ref in (image, mask):
            if ref is not None:
                ref.release()
        await remote.files.remove(staged)
```

`Path` 节点输入只传路径对象，字符串路径指远端文件；它们不会自动读取客户端文件。Python `source=...` 上传代码 artifact，图片和模型使用 `files.upload`。文件必须保留到最后一次原生读取完成，再显式删除。

## 从原生文件结果下载

```python
saved = cf.node("SaveImage", images=image, filename_prefix="comfier/result", return_call=True)
file = await saved.file()
await cf.files.download(file, "result.png")

# 捕获图的标准输出节点：需要文件/UI 时显式保留 Call
run = cf.submit(workflow, return_call=True)
files = await run.files(node_id=saved_in_graph.id, key="images")
for index, file in enumerate(files):
    await cf.files.download(file, f"result-{index}.png")
```

`Call.files(*, node_id=None, key=None)` 等待调用完成，使用该 Call 原本的 Client 将标准 UI filename/subfolder/type descriptor 定位为 RemoteFile tuple；不依赖新的当前 Client。所有执行 Call（包括 workflow run）都有此方法，但只有标准 UI 文件描述符会产生文件；没有文件时 files() 返回空 tuple，file() 在零个或多个时均报 ValueError 并提示使用 files()。不会从 Tensor、模型对象、普通字符串返回值推断文件。尚未提交的 capture Call 调用此方法会报 NotSubmitted。

无需手解 SaveImage 的 UI 字典、拼 subfolder/filename 或额外 stat。该桥接支持 input/temp/output 根，仍保留原始 metadata；只解析与查询文件元数据，不取 Tensor、不自动下载、不转换图像。自定义非标准 UI 字段仍由应用解释；已有 UI Mapping 可用 `await cf.files.from_ui(ui, node_id=..., key=...)` 走相同桥接。

## 公共方法与本地值

| 方法 | 选择与返回值 |
| --- | --- |
| `await files.upload(local_path, *, directory=None, root=None, path=None, recovery_timeout=None)` | 从本地常规文件上传，返回 `RemoteFile`；默认 input 根和 UUID 文件名，保留合法后缀 |
| `await files.download(remote_file, local_path, *, directory=None, root=None, overwrite=False, recovery_timeout=None)` | 下载并校验后发布到本地，返回 `DownloadedFile`；覆盖现有本地目标必须显式 `overwrite=True` |
| `await files.stat(remote_file, *, directory=None, root=None, checksum=False)` | 返回 `RemoteFile`；默认只读元数据，`checksum=True` 才完整扫描文件计算 SHA256 |
| `await files.resolve(descriptor)` | 在 daemon 当前允许的精确根内重新定位 `FileDescriptor` 或其保存的 Mapping，完整扫描并核对大小、SHA256，返回新的 `RemoteFile` |
| `await files.remove(remote_file, *, directory=None, root=None, missing_ok=False)` | 显式删除一个文件，返回 `None` |
| `await files.roots(*, directory=None, category=None)` | 返回 `tuple[FileRoot, ...]`；无筛选时列出 input、temp、output 三根 |

路径选择参数 `remote_file` 可以是 `RemoteFile`，或根内使用 `/` 的相对字符串。字符串默认选择 input，可显式给 `directory="input"|"temp"|"output"` 或 `root=FileRoot`；后两者互斥。`RemoteFile` 已带根与 revision，不能再附加根选择。

`RemoteFile` 包含 `root`、`relative_path`、`native_name`、`size`、可为空的 `sha256` 和 `revision`。`.name` 是 `relative_path` 的别名；**交给 ComfyUI 节点使用 `.native_name`**，其中包含 temp/output 所需的原生目录标注。上传及完整下载后 SHA256 已知，普通 stat 不暗中读取大模型。

`FileRoot` 包含不透明 `id`、远端 `path`、`directory`、`category` 和原生查找顺序 `priority`。这些是当前 daemon 文件配置的定位信息，不能作为跨 daemon 的持久 ID。`DownloadedFile` 包含本地绝对 `local_path`、`size`、`sha256` 和更新后的 `remote` 元数据。这些不可变本地值不保留 Client，没有 `fetch()` 或 `release()`。

Comfier 的默认 temp 根为原生 `base_path/.comani/temp`，冷态文件 API 与运行中的节点使用同一路径，避免另一个官方 ComfyUI 服务启动时清空 `base_path/temp` 影响当前计算。可显式传入原生参数 `--temp-directory <目录>`，实际 temp 根为 `<目录>/temp`。`FileDescriptor` 绑定其记录的根，不会随配置变化自动指向其他目录。同一 base 的多个 Comfier 进程仍共享默认 temp；需要分别保留临时文件的实例应各自设置不同的 `--temp-directory`。这不隔离 GPU 或其他共享目录。

## 模型多根与命名

模型类别和路径来自实际 ComfyUI `folder_paths` 配置，包括额外模型路径。运行中取得 live snapshot，停止后使用 last-observed；冷实例通过只加载原生路径配置的 helper 发现，既不导入 Torch，也不导入插件，不能凭空发现插件运行时新增类别。原生路径源码/YAML 变化触发重新发现，发布前复查所选 root 的配置；模型下载另按[模型发布规则](models.md)核对目标目录身份，避免无关配置变化造成重传。路径/category/directory tuple 未变的 root 保持稳定 ID；已观察移除的 ID 退役，重新注册同一路径会取得新 ID。上传到模型类别时，先取根并显式选择：

```python
roots = await remote.files.roots(category="diffusion_models")
selected = [root for root in roots if root.path == SERVER_MODEL_ROOT]
if len(selected) != 1:
    raise ValueError("Select one remote model root")
model_file = await remote.files.upload(
    "model.safetensors", root=selected[0], path="my-model.safetensors",
)
model = remote.node("UNETLoader", unet_name=model_file.native_name, weight_dtype="default")
```

按部署者的路径策略选择根；不能把 `directory` 写成任意模型类别。上传 create-only，不覆盖现有远端文件。模型类别的任一根已有同一相对名时，上传拒绝，避免在更高优先级根中制造 shadow。模型元数据还核对原生查找实际选中该文件，并在上传/删除后失效该类别的文件名缓存。此入口不安装代码或 datasets，拒绝 `custom_nodes` 和 `datasets` 类别。

远端路径拒绝绝对路径、`..`、反斜杠及跨平台非法文件名。原生配置明确指定的根先解析为 canonical 路径；其后目录及目标拒绝 symlink/reparse point，只操作常规文件。该边界不隔离具有 daemon 同等文件权限的恶意代码或外部进程。

## 流式传输与提交点

HTTP 上传和下载按 1 MiB chunk 读取文件；磁盘操作放到 I/O executor。正常成功路径单遍读取并计算 SHA256，不无条件预哈希、不整文件缓冲。上传验证大小与内容后，将同目录 `.part` 原子发布。create-only 发布需要同文件系统 hard link，否则报 `UnsupportedFileSystem`。已存在文件不会因上传失败被覆盖。

模型获取还有一个内部的已核验 stage 发布路径：私有、daemon 所有且独占的 stage 在同文件系统上直接 hard link 到 create-only 目标，完成后移除 stage 名字；实际 `EXDEV` 才使用有界复制并重新核对 SHA256。这个内部证明对象不接受任意外部 stage 路径，也没有新增公共 RPC 文件选择器。它消除模型发布的第二份整文件分配，不改变上述客户端 upload 的提交规则；跨文件系统空间和 HF acquisition cache 仍需另行计算，见 [模型设计](models.md)。

下载在服务端单遍读取并计算 SHA256，客户端验证长度、SHA256 和完整结束事实后才发布本地目标。默认 hard link create-only；`overwrite=True` 用原子 replace。源文件 revision 在打开和读取结束时检查；传入已有 RemoteFile 还核对其 revision 和已知 SHA256。revision 是 device/inode/size/mtime/ctime 的乐观变化检测，不是强快照或对具有同等文件权限的并发写入者的隔离。

`RuntimeConfig.max_file_bytes=None` 默认不添加任意大小限额，可通过显式 config/CLI/API 设置；握手 file_limits 公布有效 policy。模型 snapshot 的 max_total_bytes 是独立总 staging 限额，见 [模型设计](models.md)。实际磁盘和网络约束仍适用。

## 自动字节续传

声明 files_resume=1 的 daemon 上，await upload/download 在可恢复网络故障后自动接续同一个传输，无需用户保存 key/票据。身份绑定 daemon file epoch、签名 admission window、稳定 transfer ID、原选择与源 revision。上传只按 daemon 确认 offset 接续，commit 丢回复返回原 receipt；下载核对 exact 206/Content-Range/epoch 后才 append。未知 receipt、过期 window 或 daemon 换代明确失败，不重建一个新身份补跑。

正常 upload 单遍读；恢复时仅重读已确认前缀以重建本地滚动 SHA，并检查原路径和打开文件 revision。恢复下载可重读服务端前缀；最后校验/EOF 回复丢失时可能需要一次恢复专用全文件扫描。最终大小、whole SHA 与原子发布仍必需；不盲 append，不仅凭 length 认成功。

recovery_timeout 默认使用服务器 transfer_idle_timeout=3600 秒，从观察到连接故障开始计时，健康进展重置。它不是整个成功传输的总 deadline；底层 HTTP timeout 仍控制一次请求和故障检测。证书/TLS 校验、路径、revision/hash、未知/过期身份错误不作为可恢复故障反复重试。

默认 active transfer 全局128、每认证owner8，满额以 FileTransferBusy 背压，SDK在原窗口内重试接纳。无活跃 partial 被静默驱逐；retained metadata 上限为 max(1024,8×transfer_limit)，receipt不消耗active slot但仍受总metadata限额。idle清理独立于计算session宽限；可显式配置 transfer_idle_timeout/transfer_limit/transfer_owner_limit。

显式取消本地 task 或 Client.close 停止文件 I/O，等待活动磁盘工作收尾并尝试有界清理自己的未发布 partial；不可达时由 daemon idle TTL 清理。取消若晚于发布点，完整文件可能已存在，不能声称回滚。目录可能保留。续传只覆盖活动 await 与活着的 daemon registry，不是跨客户端进程/daemon崩溃的持久传输数据库。

无 resume capability 的旧服务器保留单次 multipart/framed 路径和失败清理语义。非 HTTP 自定义 transport 流式 upload/download 明确 UnsupportedTransport；roots/stat/remove 仍取决于对应 RPC。目录上传可用普通 Path.rglob 和有界 asyncio.gather 调度文件，不提供虚构的 sync API。

## 持久性与会话

已完成文件由原生文件系统持有，可供其他授权会话使用，不属于 session 的 owned refs。GC、`RemoteRef.release()`、Client close 和 daemon 正常关闭都不删除已完成文件；temp 根的后续清理由原生环境或部署者策略决定，Comfier 不提供永久保留保证。删除通过 `files.remove()` 明确进行；传入 `RemoteFile` 时会拒绝删除 revision 已改变的文件。

保存 `files = remote.files` 会强持有所属 Client；Client 每次访问都返回新代理，不缓存它，因此没有反向所有权环。只保存 `RemoteFile`/`FileRoot`/`DownloadedFile` 不保活会话。显式 Client close 优先取消活动文件 I/O，关闭后需新建 Client 才能再次操作文件。

## 保存文件描述并在新 daemon 中核验

`RemoteFile` 没有 Client，但其中 `root.id` 仍属于当前 daemon 的文件配置实例；管理 daemon 重建后，不能把旧 ID 当成持久授权。应用可以显式保存 `FileDescriptor`，在新的 Client 中重新取得当前有效的 `RemoteFile`：

```python
import json
from dataclasses import asdict
from pathlib import Path
import comfier as cf

# 在成功保存文件后，记录已核验内容。普通 stat/from_ui 的 SHA256 可能为空。
async with cf.Client(url, token=token) as remote:
    file = await remote.files.stat("shots/front.png", directory="output", checksum=True)
    Path("front-file.json").write_text(json.dumps(asdict(file.describe())), encoding="utf-8")

# 可以在另一个 Python 进程、计算重启或管理 daemon 重建后执行。
async with cf.Client(url, token=token) as remote:
    saved = json.loads(Path("front-file.json").read_text(encoding="utf-8"))
    file = await remote.files.resolve(saved)
    await remote.files.download(file, "verified-front.png")
```

`file.describe()` 是本地元数据转换，不执行 I/O；SHA256 为空时明确报 `ValueError`，要求先执行 `stat(file, checksum=True)`。上传或完整下载得到的 `RemoteFile` 已有 SHA256，可以直接 describe。`FileDescriptor` 是无 Client 的不可变 dataclass，字段为 `version=1`、`directory` 或 `category`、服务端已规范化的 `root_path`、`relative_path`、`size` 和必需的 `sha256`；用 `dataclasses.asdict` 和 JSON 保存，也可通过 `cf.FileDescriptor(**saved)` 重建。它不保存 session、root ID 或 inode/revision。

`files.resolve` 是真实文件 RPC，复用 daemon 的 stat 实现，始终完整读取文件计算 SHA256。daemon 仅在当前原生配置允许的目录或模型类别中匹配精确 `root_path`，然后检查相对路径、常规文件、大小和 SHA256。保存的路径是定位约束，不授予任意服务端目录权限；客户端不使用本地操作系统解释远端路径。根已更换、目录不再允许、文件缺失、路径越界、内容变化或未知描述版本均明确失败，不搜索全盘，也不改绑到其他根的同名文件。旧 daemon 不支持描述参数时会拒绝解析，不降低为仅按名称查找。

这是对已知位置和预期字节的重新核验：同位置恢复为相同内容的文件可以通过，成功返回的新 revision 仅用于后续文件操作的变化检测。它不保证文件不可变、永久保留或核验后不会再变化；并发修改仍受现有 revision 检查约束。根迁移需要应用显式选择新位置并重新核对，描述本身不自动搬运文件、不提供全局资产数据库，也不让 tensor、模型对象或 `RemoteRef` 跨 Client 持久化。
