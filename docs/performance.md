2026-11-16 范围说明：本页下文完整保留 2026-11-15 的历史候选、计时和失败记录。新的管理架构、普通磁盘 stage 发布、冻结 H3 脚本与 GPU/WAN 身份见 [管理验收](management_acceptance.md)，线程配置、冷热边界及 eager/capture 差值分析见 [本轮性能审阅](management_performance.md)。这些新记录不认证当前工作区中待定的输出 API。

# 性能：计算、结果取回与持续生成

2026-11-15 的最终实测结论是：**冻结候选的 files、兼容传输与真实 H3 生成通过；健康 RTX 4090 的热采样稳定，但 max-2 流水和 LZ4 均未证明稳定端到端提速。** 实际 HTTPS 入口的 gzip JSON 比 binary identity 和 LZ4 更小，不能把移除 base64 直接等同于节省 WAN 流量。

最终远端测量使用生产候选 `cae8c24`、ComfyUI `eee59185`、H3 INT8 2048×2048、batch 1、6 steps、`--reserve-vram 4`。本页将旧 host 受限记录与换 host 后的独立运行分列；全样本和来源 SHA256 见[最终远端摘要](../artifacts/performance/remote-2026-11-15.json)。早期 `cd6f45c8` 的三路径诊断与本地 codec 实验保留在后文及[原证据摘要](../artifacts/performance/2026-11-15-summary.json)，[历史远端报告](remote_acceptance.md)保留原始构建身份。

## 本轮候选身份与本地验收

版本仍为 `0.3.0`，候选 wheel 为 `comani-0.3.0-py3-none-any.whl`。下列身份由[生产清单](../artifacts/performance/product-manifest.json)、[78 项功能冻结清单](../artifacts/performance/final-feature-freeze.json)与[独立本地验收摘要](../artifacts/performance/local-acceptance.json)关联：

```text
wheel SHA256   38ddaf7d879c39e5402ecbad554b41bf4499138a10aa3e1e00707359908f58ee
freeze SHA256  88e6b57429b8aaaa8d8f1b2e1c40510b2284f657cacda2180c644c7a4dc05126
manifest SHA256 26d2ca637b98f3b03fd6f7e491d569d5630cbde302c57d896d74f257619e8e49
```

独立验收核对 21 个 package Python 文件在 source、wheel 和三个安装环境中一致；core 客户端无 Torch、ComfyUI 或 LZ4，optional 客户端安装 LZ4。真实 CPU native HTTP 验证包含 input/temp/output 上传后的 9 次 LoadImage、原生模型第二根、原子下载 SHA、元数据与代理所有权、独立 heartbeat、重复取消与同 Client 恢复、损坏 LZ4 一次 fetch 无重试，以及真实 `42350b0` 与新版本两向兼容。12 MiB 非连续 synthetic CPU tensor 的 JSON/binary/LZ4 保持 dtype、shape、stride、alias、device 和逻辑 SHA；它只证明正确性。

完整适用 CPU suite 单轮结果为 **305 passed、1 failed、2 deselected**。唯一失败经独立复现：测试监听全 app 的最后响应，正确 binary fetch 后的 heartbeat JSON 污染了断言。作者只修该测试文件的 observer，随后 **29 项 LZ4 测试通过，原 case 重复 200/200 通过**；没有把它改记为一次 306 passed。最终 wheel 相比完成产品实测的 `e3c255d6…` 原件仅变更 README 的 METADATA 及其 RECORD，重包后复核身份、导入与 CLI，无重复产品测试。所有验收子进程、端口及 session/call/object/request/upload/partial 计数已核清。

上述本地结论限于 WSL/Linux CPU；本轮真实 GPU/WAN 结果如下，真实 Windows/NTFS 仍未测试。

## 最终远端：真实脚本、出图与热生成

两轮均安装同一个已验 wheel，核对 21 个 package Python 文件、README metadata 和公开源码清单，使用同一套已核 SHA256 的模型、官方模板镜像 tag `vastai/comfy:v0.44.0-cuda-13.2-py312`、Python 3.12.15、Torch 2.10.0+cu130、LZ4 4.4.5 与原生 flags。镜像 immutable digest 未暴露；CPU、driver、power limit、线程数、RAM 限额与 observer 方式不同，不能声称两台环境逐字节相同。

实际公共脚本是 [冻结的 examples/minimax_h3_reuse.py](https://github.com/Delva0/comani/blob/cae8c24ff13f97256d0b6b79a361c7ba1fbcacde/examples/minimax_h3_reuse.py)，SHA256 `cd1e1d5d…`。独立验收 driver 原样 import 其 `setup/run_images`，编排 warmup、同 ref 传输与 ABBA；driver 是验收工具，不是另一个产品入口。两轮原始 driver 与来源 hashes 保留在最终摘要。下面是健康 host 的实际新样图，完整 float tensor 校验另存于验收证据；PNG 不是 float 对象的替代传输。

![冻结候选在健康 RTX 4090 上生成的 H3 2048×2048 样图](../artifacts/performance/final-remote-2026-11-15.png)

| 阶段 | 旧 host，受限记录 | 健康 host |
| --- | ---: | ---: |
| 稳定 refs setup | 208.584 s | 6.469 s |
| 首次 generation，不含 setup/fetch/save | 106.949 s | 10.692 s |
| 其中 warmup KSampler | 83.134 s | 8.672 s |
| 脚本首图：client 函数开始至保存 | 328.726 s | 27.607 s |
| 后续 8 张新 seed 的 KSampler | 5.764–33.165 s | 3.367–3.473 s |

首图计时不含租机、部署或模型下载；setup 是模型冷态准备，不是 daemon 启动总时间，也没有清 OS/JIT 磁盘 cache。健康轮 warmup 加 8 张不同 seed 均实际执行 6 次 forward，并记录 GPU 完成屏障，不是把旧 cache hit 当生成吞吐。

ABBA 为 serial、max-2、max-2、serial，每块两张新 seed；块时间包含计算等待、fetch 与保存，排除先前 setup/provenance：

| 顺序 | 模式 | 旧 host 块时间 | 健康 host 块时间 |
| --- | --- | ---: | ---: |
| A1 | serial | 67.632 s | 22.437 s |
| B1 | max-2 | 70.377 s | 19.314 s |
| B2 | max-2 | 75.112 s | 23.228 s |
| A2 | serial | 65.047 s | 20.794 s |

健康轮 max-2 汇总约快 1.6%，但两块方向不一致，样本少且网络 fetch 波动明显；旧 host 的 max-2 更慢。没有稳定吞吐收益或单张延迟改善的证据。该示例展示显式 refs 复用与有界资源窗口，`--serial` 保留直接对照，不产生 GPU 节点并行承诺。

旧 host 的连续 `nvidia-smi` 20 次达到 10 秒 timeout；停止旧 worker 后 PID 已消失，仍记录约 20,284 MiB board 显存，另一次 5 秒有界查询也超时，因此未创建第二 worker，未完成同机重启对照。健康 host 初始空闲 GPU 查询约 22 ms，负载中不做外部 GPU 轮询，只采轻量 native/proc 数据。旧长首步/尾等待在新轮未复现，但多个条件同时改变，不能把改善单独归因于停 NVML、CPU、driver 或 power，也不能据此认定历史异常根因已修复。

## 最终远端：同一真实图像的传输

健康轮在同一 ready ref 上交错取回，每模式 3 次。所有结果的 shape、dtype、stride、CPU device 与逻辑 tensor SHA256 一致。HTTP entity 捕获关闭自动解压；实际链路为官方模板 HTTPS quick tunnel 经 Caddy 到 daemon，不是独立带宽实验，也未计物理 HTTP/TLS/TCP framing。

| 方式 | HTTP 编码 entity | HTTP 解码后 entity | SDK fetch 全部样本 | 中位数 |
| --- | ---: | ---: | --- | ---: |
| 兼容 JSON + Caddy gzip | 39,663,758 B | 67,109,487 B | 3.994 / 4.289 / 3.850 s | 3.994 s |
| binary identity | 50,332,082 B | 50,332,082 B | 4.243 / 3.719 / 4.550 s | 4.243 s |
| binary LZ4 | 44,664,470 B | 44,664,470 B | 4.300 / 4.418 / 3.631 s | 4.300 s |

LZ4 解压后仍为同一 50,332,082 B pickle，vendor media type 无 HTTP Content-Encoding；JSON 有 gzip。这张真实图像的 LZ4 比 binary identity 少约 11.26%，但比 gzip JSON 更大。SDK fetch 包括传输、协议解码和 unpickle；小样本中位数也未显示 LZ4 更快，不能挑 3.631 s 最快一次推导普遍收益。客户端 CPU 样本与含两次 probe dispatch 的服务端进程 CPU 增量完整留在 JSON，不将其当纯 codec CPU 计时。

旧 host 的 n=2 样本也保留：JSON **7.249 / 7.295 s**，identity **8.332 / 7.962 s**，LZ4 **6.563 / 4.370 s**；该轮 gzip JSON entity 为 **39,663,759 B**。它不足以推翻健康轮结果或建立压缩的稳定 WAN 加速。二进制保留对象语义并减少 JSON/base64 CPU/内存步骤，其网络收益仍取决于真实入口压缩与链路。

### 场景选择：只需要最终 PNG

需要精确 float Tensor 时仍使用 `ref.fetch()`。若业务只需要 8-bit PNG，可显式在远端编码保存，再通过 files 下载，避免先把约 50 MB float IMAGE 传到客户端编码。当前 `Call` 没有公开的 SaveImage UI/文件名访问接口，不能写成 `call.ui` 或假设 await 返回文件路径；固定 backend 的 SaveImage 输出槽仍是 IMAGE。

现有 `remote.python(source=...)` 可显式调用原生 SaveImage 并仅返回它的文件元数据。下面将 helper 存为客户端本地 `save_png.py`，针对本页 batch 1；它在远端运行，不是新增 SDK 功能：

```python
def save_png(image):
    from pathlib import PurePosixPath
    from uuid import uuid4
    from nodes import SaveImage

    if image.shape[0] != 1:
        raise ValueError("This export expects batch 1")
    saved, = SaveImage().save_images(
        image, filename_prefix="comani-" + uuid4().hex)["ui"]["images"]
    return str(PurePosixPath(saved["subfolder"]) / saved["filename"])
```

在已有 Client 中，把解码后的远端 IMAGE 引用传给 helper：

```python
from pathlib import Path

saved = remote.python(source=Path("save_png.py"), entry="save_png",
                      inputs={"image": decoded.output(0)})
name_ref = await saved.output()
try:
    name = await name_ref.fetch()  # 只取小文件名
finally:
    name_ref.release()
file = await remote.files.stat(name, directory="output")
await remote.files.download(file, "result.png")
```

这是调用方明确选择原生 PNG 的量化/裁剪契约，并非精确 Tensor 的无损替代；文件仍按 files 的持久性与显式清理规则处理。此路径已按当前 SDK 与固定 ComfyUI `eee59185` 的 SaveImage 返回结构核对，但未做新的 GPU 执行或干净 WAN 速度对照，不据此承诺比 float fetch 更快。

是否公开原生 Call metadata 以便取得结果文件名，是后续可选 API 缺口；本轮未选定或实现，不阻塞当前文件路径。

## 最终远端：files、长任务与清理

旧 host 上独立完成功能闭环：真实 input/temp/output 文件、LoadImage/captured ImageInvert、下载 SHA、create-only、取消清理与同 Client 恢复、跨 session 持久性、显式删除，以及原生模型多根同名拒绝与约 15.7 GB 模型默认 stat 无 hash。**129 MiB（135,266,304 B）** 公网流式上传超过旧 RPC 128 MiB 上限，通过后明确删除；它不是同大小往返下载吞吐报告。core 客户端没有本地 Torch/LZ4，两个 host 都通过相同 WAN 的 generic binary identity 与文件路径验证。

同旧入口的 130 秒任务实际工作 **130.000 s**，端到端 **138.245 s**，只有 **1 submit、7 wait_result、1 fetch**。这验证有界等待而非自动重提；它未与 model I/O 并行执行，不提供两者重叠证据。健康轮复测范围是冻结身份、core WAN、真实 H3、传输 n=3 与 ABBA；未把旧轮 129 MiB 和 130 秒检查改记为新 host 重跑。

两台实例均已销毁，并分别在 **11:54:38 UTC / 12:17:42 UTC** 通过官方实例列表空结果复核，清理证据在最终摘要。新轮实际 cgroup RAM 限额 **103.74 GiB**，H3 窗口 RSS 采样峰约 **25.43 GiB**、cgroup current 峰约 **46.93 GiB**，无 memory max/OOM 事件；这不是硬 64 GiB 验证或最低 RAM 需求。负载中未采板级 GPU 显存峰，不能用 Torch allocator 代替，也不报告可靠峰值 VRAM。

## 早期三路径诊断：计算与取回分开看

以下是 `cd6f45c8` 的历史诊断，详细数值仍在[原证据摘要](../artifacts/performance/2026-11-15-summary.json)。eager、captured graph、raw API JSON 三条路径都经 Comani RPC→NativeAdapter→原生 PromptExecutor；raw JSON 不是独立 ComfyUI `/prompt` 对照。eager 在 await 前已提交，await 等就绪，fetch 才取回本地。

历史顺序端到端为 **177.788 / 152.406 / 13.478 s**。当时换机器运行原样脚本、无插桩为 **25.989 / 12.775 / 8.469 s**，该轮未复现百秒异常。两机 normalized dependency freeze 一致，但 CPU/driver/host 不同，缺历史 immutable image digest，不能断言容器字节相同或某 SDK/host/compiler 根因已修复。

有效同 seed 插桩中，首轮 eager native **18.029 s / 6 forwards**，后续 capture **5.323 s / 6 forwards**，最后 raw **0.968 s / 0 forwards**。后者已命中上游采样缓存，不能算三种 API 的加速比。eager→capture 的 **12.705 s** native 差由 loader **1.906**、文本编码 **4.856**、采样 **5.228**、decode **0.711 s** 闭合；原生节点外壳仅约 **0.025 / 0.013 s**，未显示重写调度器的依据。

首 forward **5.261 s** 中 `pin_modules` **4.926 s**，包含权重物化、转换、host pin、同步及量化处理，不能全称 PCIe 复制；它嵌在 prefetch span 内，不能相加。这里“冷”只指 fresh daemon/CUDA process，模型已下载，未清 OS/driver/JIT 磁盘 cache。

复用 model、conditioning、latent、negative、VAE owned refs 后，仅变 seed：

| 真正生成新图 | native worker | forwards |
| --- | ---: | ---: |
| eager 复用，第 1 / 2 张 | 4.282 / 3.789 s | 各 6 |
| warm captured graph | 3.789 s | 6 |
| warm raw JSON | 3.898 s | 6 |
| 相同 raw 输入再运行，缓存命中 | 0.0116 s | 0 |

历史复用 setup **2.834 s** 发生在已热 runtime，不是 fresh 启动成本。结果为 CPU tensor，末尾 GPU barrier 约 **0.00011–0.00013 s**。两张复用的端到端 **14.965 / 12.642 s** 中，fetch **8.601 / 6.349 s**、body read **7.327 / 5.144 s**，说明该轮主要等待已转向结果物化。

首轮三路 fetch 的 header/body/JSON/base64-unpickle 分别约 **1.194/4.328/.126/.165**、**.922/4.082/.132/.151**、**.873/3.509/.131/.142 s**；服务端 pickle/base64 **.253/.239/.207 s** 已含在 header 等待中。约 **67,109,487 B** 是 aiohttp 读取后的 JSON payload，因未保留 Content-Encoding/编码长度，不能当 wire 流量。

## 现有做法与合理的工程投入

[当前 H3 性能基准](../benchmarks/minimax_h3_reuse.py)一次准备稳定 refs，随后逐 seed 采样；默认最多一张正在计算、上一张正在 fetch/save，`--serial` 可关闭重叠。它保留原生模型管理和单 native worker，通过小窗口限制未物化结果的数量。改变 prompt、尺寸或模型时，应重新准备相应 refs，并显式释放不用的 refs。

当前例程已迁入显式管理 API。下面入口用于已配置实例，完整环境/模型准备参数见[H3 主流程](design/development.md#h3-完整脚本)。历史测量使用各报告记录的旧脚本 hash，不因示例改写成为本轮管理架构的性能证据。例如：

```sh
comfier daemon --comfy-root /path/to/ComfyUI --runtime-python /path/to/runtime/bin/python --no-start -- --reserve-vram 4
```

客户端配置 `COMFIER_URL`、`COMFIER_TOKEN`，在项目目录运行。脚本通过本地 models.resolve 查询来源、远端 models.download 校验并复用指定三模型，不再读取私有 model-verification 文件：

```sh
python -B benchmarks/minimax_h3_reuse.py \
  --output artifacts/h3-overlap --seeds 426390 426391 426392
python -B benchmarks/minimax_h3_reuse.py \
  --serial --output artifacts/h3-serial --seeds 426390 426391 426392
```

原 --verification-path 已移除；环境/源码事实来自 comfyui.info，模型校验来自 daemon 管理接口。--serial、原 seed 默认值和逐张精确 Tensor fetch/save 路径保留，新增 preparation_seconds 单列模型/环境检查成本。例程写出 fetch/save/latency 与整个运行吞吐；其本地依赖仍为 Torch/NumPy/Pillow。上述历史 GPU 结果没有自动覆盖这次改写，需以新代码重新验收。重叠是吞吐候选，不代表 GPU 节点并行或单张延迟必然降低。

合理顺序是先复用、再测小窗口流水、再改善显式数据面。当前证据不要求新调度器、隐式 Python memoization 或替换原生显存管理；compiler/offload flags 应在可复现瓶颈上单独做 A/B。

## 二进制与压缩的证据边界

binary fetch 直接返回原有 cloudpickle bytes，去掉 JSON/base64 包装；它保留完整对象语义，没有新增 tensor 专用 codec、精度转换、缩图或 PNG 替代。握手能力协商、新旧双向 JSON fallback 与 LZ4 校验规则见[协议](design/protocol.md#二进制结果取回)。本地正确性、兼容性及本轮真实 WAN 结果分列；以下本地实验不能替代前面的最终远端测量。

本地 loopback 的 **synthetic `torch.arange` CPU tensor** 测量为：

| 方式 | HTTP entity body | SDK fetch 中位数 |
| --- | ---: | ---: |
| JSON/base64 | 67,109,479 B | 0.6723 s |
| binary pickle | 50,332,077 B | 0.1473 s |

两者均无 Content-Encoding；抓取 body 时关闭了自动解压。这是 HTTP entity bytes，不含 HTTP/TCP framing，也不是物理 wire bytes。该样本不是生成图片，本地结果不能推算 WAN/GPU 提升，也不能与可能已经 gzip 的旧 JSON 响应直接比较。

另一个探索性本地 CPU 实验对**已保存的真实 H3 图像**使用同一份 **50,332,080 B** pickle payload，结果如下。每次解压均做完整字节与 SHA256 一致性检查；该实验的 LZ4 frame 尚未启用生产协议要求的 content checksum。

| codec | 编码后大小 | encode 中位数 | decode 中位数 |
| --- | ---: | ---: | ---: |
| identity | 50,332,080 B | 无 codec 成本 | 无 codec 成本 |
| gzip level 1 | 29,157,756 B | 1.295 s | 0.331 s |
| gzip level 6 | 28,171,254 B | 2.142 s | 0.329 s |
| LZ4 frame fast | 42,363,544 B | 0.101 s | 0.043 s |

这组数据来自 i7-14700HX/WSL2 的单调用线程，不含序列化、HTTP、unpickle 或 GPU。PNG 和高熵 bytes 基本没有压缩收益，部分会膨胀。以“节省字节 ÷ encode+decode”算出的串行无重叠盈亏点只是理论模型，不能当远端实测。

生产实现采用可选安装 `comfier[lz4]`、两端精确能力协商、缺失能力回退 binary identity。`cf.Client(..., fetch_compression="auto")` 默认允许协商；`"identity"` 禁用 LZ4。提供自建 transport 时，将选项写在 `HTTPTransport` 上。`auto` 不自动测网络：至少 1 MiB 才尝试压缩，且压缩后至少节省 1% 才发送。

带 content checksum 的生产 codec 对同一真实图像 payload 编码为 **42,363,548 B**，节省 **15.83%**；本地 encode/decode 中位数为 **0.115 / 0.054 s**，合计约 **0.169 s**。生产 HTTP loopback 的交错单 fetch 样本为 identity **0.109 / 0.113 s**，LZ4 **0.262 / 0.261 s**，在该快链路上更慢。计时包括 HTTP、压缩和解压，排除 cloudpickle serialization/unpickle，不能当作完整 SDK 或 WAN 延迟。

这轮抓取关闭了自动解压：identity/LZ4 的 HTTP entity body 分别为 **50,332,080 / 42,363,548 B**，均无 Content-Encoding；这也不是物理 wire bytes。高熵 bytes 和已压缩 PNG 未达到节约阈值，返回 identity，但尝试压缩仍消耗 CPU 并暂时分配完整候选 body。二进制及 LZ4 均保留整块 pickle 的内存成本；这份先前保存的图像与最终远端样图不是同一个 payload。

显式 PNG 导出也需要单独衡量：首次 probe 总计 **6.595 s**，其中 await **3.624 s** 已含 encode **2.379 s** 和 helper 首次源码上传；同图 float fetch 加本地 save 约 **6.020 s**。这是不同阶段的有限样本，不能证明稳态 PNG 更快或更慢。PNG 文件约 **4.82 MB**，RPC 解码后 JSON payload 约 **6.42 MB**，同样不是已测 wire bytes；PNG 的 8-bit 输出契约也不能静默替代 float tensor。

## 本地图片与模型文件

本轮公共入口为 `await remote.files.upload("local.png")`，直接返回本地文件元数据，再把 `.native_name` 交给 `LoadImage`。它使用独立流式 I/O，上传、下载都单遍读取并做尾部 SHA256 验证后发布，不通过 Python 代码 artifact 或整文件 pickle/base64 staging。

默认 stat 不扫描大文件；`checksum=True` 才显式计算 SHA256。模型上传先列出并明确选择原生类别根，不能覆盖或制造同名 shadow。文件共享且持久于 session，GC/Client close 不删除；取消晚于提交点可能已留下完成文件。调用签名、持久性和边界集中在[文件 I/O](design/files.md)。

旧项目可借鉴显式数据面、能力协商和文件生命周期独立于引用；其整块序列化、进程 LRU 强持有、变更后复用旧序列化与临时文件清理缺口不迁入当前设计。旧 preview 的 batch[0]/uint8/缩图契约也不能替代精确 Tensor。

## 尚未建立的保证

没有最大吞吐/最低资源保证、真实 Windows/NTFS 认证、跨 host 单因素因果结论或所有网络上的压缩收益。历史 H3 只测 batch 1，模型/节点/入口变化需各自验证。带外管理、Operation、同代计算恢复和 files 字节续传已实现，见[管理设计](design/remote-management-plan.md)与[文件 I/O](design/files.md)；本文历史测量仍绑定其原构建，不充当新架构的GPU/WAN验收。
