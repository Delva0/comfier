# 性能：计算、结果取回与持续生成

2026-11-15 的实测结论是：**显式复用不变输入后，eager 的真实新 seed 计算与热态 graph/raw JSON 同档；结果取回比计算更耗时。** 优先工作是复用示例、有界地重叠计算与结果物化、减少数据面开销。

真实 GPU 数据对应 Comani `cd6f45c8`、ComfyUI `eee59185`、RTX 4090、H3 2048×2048、batch 1、6 steps、`--reserve-vram 4`。本轮 binary/LZ4/files 与复用例程在 `42350b0` 上实现，独立本地验收已通过；组合包真实 GPU/WAN 验收仍未完成。精选数值和来源 SHA256 见[证据摘要](../artifacts/performance/2026-11-15-summary.json)；[历史远端报告](remote_acceptance.md)保留原始构建身份。

## 本轮候选身份与本地验收

版本仍为 `0.3.0`，候选 wheel 为 `comani-0.3.0-py3-none-any.whl`。下列身份由[生产清单](../artifacts/performance/product-manifest.json)、[78 项功能冻结清单](../artifacts/performance/final-feature-freeze.json)与[独立本地验收摘要](../artifacts/performance/local-acceptance.json)关联：

```text
wheel SHA256   38ddaf7d879c39e5402ecbad554b41bf4499138a10aa3e1e00707359908f58ee
freeze SHA256  88e6b57429b8aaaa8d8f1b2e1c40510b2284f657cacda2180c644c7a4dc05126
manifest SHA256 26d2ca637b98f3b03fd6f7e491d569d5630cbde302c57d896d74f257619e8e49
```

独立验收核对 21 个 package Python 文件在 source、wheel 和三个安装环境中一致；core 客户端无 Torch、ComfyUI 或 LZ4，optional 客户端安装 LZ4。真实 CPU native HTTP 验证包含 input/temp/output 上传后的 9 次 LoadImage、原生模型第二根、原子下载 SHA、元数据与代理所有权、独立 heartbeat、重复取消与同 Client 恢复、损坏 LZ4 一次 fetch 无重试，以及真实 `42350b0` 与新版本两向兼容。12 MiB 非连续 synthetic CPU tensor 的 JSON/binary/LZ4 保持 dtype、shape、stride、alias、device 和逻辑 SHA；它只证明正确性。

完整适用 CPU suite 单轮结果为 **305 passed、1 failed、2 deselected**。唯一失败经独立复现：测试监听全 app 的最后响应，正确 binary fetch 后的 heartbeat JSON 污染了断言。作者只修该测试文件的 observer，随后 **29 项 LZ4 测试通过，原 case 重复 200/200 通过**；没有把它改记为一次 306 passed。最终 wheel 相比完成产品实测的 `e3c255d6…` 原件仅变更 README 的 METADATA 及其 RECORD，重包后复核身份、导入与 CLI，无重复产品测试。所有验收子进程、端口及 session/call/object/request/upload/partial 计数已核清。

这份结论限于 WSL/Linux CPU。真实 Windows/NTFS、GPU/H3 连续吞吐和 WAN 压缩收益尚未验收；旧 GPU 证据不能直接充当该候选的认证。

## 三条执行路径是什么

| 路径 | 提交方式 | 执行位置 |
| --- | --- | --- |
| eager | 每次 `remote.node` / `remote.python` 立即提交，远端引用连接依赖 | Comani RPC → NativeAdapter → 原生 PromptExecutor |
| captured graph | capture 收集显式图，运行时提交整图 | 同上 |
| raw API graph | 把原生 API JSON 交给 Comani workflow 提交 | 同上 |

这是三种表达和提交方式，共用原生执行器。这里的 raw JSON 数据不是独立 ComfyUI 服务器 `/prompt` 的直接对照。eager 调用在 `await` 前已提交；`await` 是等待输出就绪，`fetch()` 才把对象取到客户端。

历史顺序结果为 eager **177.788 s**、capture **152.406 s**、raw JSON **13.478 s**。新机器运行同一原样脚本、无性能插桩时为 **25.989 / 12.775 / 8.469 s**，旧的百秒等待没有复现。

旧慢机器与新机器的完整 normalized dependency freeze 一致，CPU、driver、host 不同；历史未记录不可变容器 image digest。现有证据不能证明所有容器字节相同，也不能把 host、compiler 或 SDK 指认为旧 150 秒异常的根因，更不能宣称已修复该异常。

## 为什么首轮 eager 慢

有效插桩的同 seed 顺序运行如下。后运行的路径继承了前一次的模型状态和原生缓存，不能直接据此计算三种 API 的加速比。

| 顺序路径 | 端到端 | native worker | 真正模型 forward | 状态 |
| --- | ---: | ---: | ---: | --- |
| eager 首轮 | 25.720 s | 18.029 s | 6 | 首次模型/encoder/sample/decode |
| capture 后运行 | 12.458 s | 5.323 s | 6 | loader 已命中，模型热 |
| raw JSON 最后运行 | 7.385 s | 0.968 s | 0 | 上游含 sample 已命中，主要重做 decode |

eager 到 capture 的 native 时间差 **12.705 s**，由 loader **1.906 s**、文本编码冷热差 **4.856 s**、采样冷热差 **5.228 s**、decode 冷热差 **0.711 s** 闭合。原生 node 外壳分别仅约 **0.025 / 0.013 s**，没有显示需要重写调度器的固定开销。

首个 forward 为 **5.261 s**，其中 `pin_modules` 为 **4.926 s**。它涉及权重物化、转换、host pin、stream 同步和量化权重处理等原生路径，不能全称为 PCIe 复制。`prefetch_queue_pop` 包含这一 span，二者不能相加；后五个 forward 各约 **0.44 s**。这一分解只解释此次冷到热的差别。

“冷”在这里仅指 fresh daemon/CUDA process：模型已下载并校验，没有清除 OS page cache、driver/JIT 磁盘缓存等，不能读作完全冷机测量。

## 稳态计算、缓存命中和取回要分别看

复用 model、conditioning、latent、negative、VAE 的 owned refs 后，仅变 seed：

| 真正生成新图 | native worker | forward 次数 |
| --- | ---: | ---: |
| eager 复用，第 1 张 | 4.282 s | 6 |
| eager 复用，第 2 张 | 3.789 s | 6 |
| warm captured graph | 3.789 s | 6 |
| warm raw JSON | 3.898 s | 6 |
| raw JSON 完全相同输入再运行 | 0.0116 s | 0 |

最后一行是结果缓存命中，不能用作生成吞吐。复用实验的一次 setup **2.834 s** 发生在已热运行时中，也不能代替 fresh process 的启动成本。取回对象均为 CPU tensor，最终 GPU barrier 约 **0.00011–0.00013 s**，没有把大段未完成 CUDA 工作留到计时外。

两张 eager 复用的端到端时间仍为 **14.965 / 12.642 s**，其中 fetch 为 **8.601 / 6.349 s**，仅 body read 就占 **7.327 / 5.144 s**。这说明热计算同档后，客户端感受到的主要等待已转向结果物化。

首轮三路 fetch 的有效分解为：

| 阶段 | eager | capture | raw JSON |
| --- | ---: | ---: | ---: |
| 等响应 headers | 1.194 s | 0.922 s | 0.873 s |
| 读 body | 4.328 s | 4.082 s | 3.509 s |
| JSON decode | 0.126 s | 0.132 s | 0.131 s |
| 客户端 base64/unpickle | 0.165 s | 0.151 s | 0.142 s |

服务端 pickle/base64 **0.253 / 0.239 / 0.207 s** 已包含在 headers 等待中，不能再加一次。该轮约 **50.3 MB** 原 tensor 对应 **67,109,487 B** 客户端读取后的 JSON payload；历史没有保存 Content-Encoding 和编码后长度，因此这个数字不是物理链路流量。

## 现有做法与合理的工程投入

[复用例程](../examples/minimax_h3_reuse.py)一次准备稳定 refs，随后逐 seed 采样；默认最多一张正在计算、上一张正在 fetch/save，`--serial` 可关闭重叠。它保留原生模型管理和单 native worker，通过小窗口限制未物化结果的数量。改变 prompt、尺寸或模型时，应重新准备相应 refs，并显式释放不用的 refs。

环境与模型由部署者准备，沿用[已有启动入口](design/development.md)。例如在具备依赖的远端解释器中：

```sh
comani daemon --comfy-root /path/to/ComfyUI -- --reserve-vram 4
```

客户端配置已有 daemon 的 `COMANI_URL`、`COMANI_TOKEN`，并准备[已校验的 H3 模型](../tests/fixtures/minimax_h3_models.json)和远端 model-verification 记录后，在项目目录运行：

```sh
python -B examples/minimax_h3_reuse.py \
  --output artifacts/h3-overlap --seeds 426390 426391 426392
python -B examples/minimax_h3_reuse.py \
  --serial --output artifacts/h3-serial --seeds 426390 426391 426392
```

若验证记录位置不同，用 `--verification-path` 指定其**远端**路径。例程写出每张的 fetch/save/latency 和整个运行的吞吐；热运行不等于省略 setup 或 provenance 成本。本地已验证实际 recipe 的串行/重叠调度、CPU 原生执行、失败与取消收尾，**真实 H3 GPU 流水吞吐仍待验**。重叠是吞吐候选，不代表 GPU 节点并行或单张延迟必然降低。

合理顺序是先复用、再测小窗口流水、再改善显式数据面。当前证据不要求新调度器、隐式 Python memoization 或替换原生显存管理；compiler/offload flags 应在可复现瓶颈上单独做 A/B。

## 二进制与压缩的证据边界

binary fetch 直接返回原有 cloudpickle bytes，去掉 JSON/base64 包装；它保留完整对象语义，没有新增 tensor 专用 codec、精度转换、缩图或 PNG 替代。握手能力协商、新旧双向 JSON fallback 与 LZ4 校验规则见[协议](design/protocol.md#二进制结果取回)。本地正确性与兼容性已有独立验证，最终组合包的验收状态见页首，远端收益仍待测。

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

生产实现采用可选安装 `comani[lz4]`、两端精确能力协商、缺失能力回退 binary identity。`Client(..., fetch_compression="auto")` 默认允许协商；`"identity"` 禁用 LZ4。提供自建 transport 时，将选项写在 `HTTPTransport` 上。`auto` 不自动测网络：至少 1 MiB 才尝试压缩，且压缩后至少节省 1% 才发送。

带 content checksum 的生产 codec 对同一真实图像 payload 编码为 **42,363,548 B**，节省 **15.83%**；本地 encode/decode 中位数为 **0.115 / 0.054 s**，合计约 **0.169 s**。生产 HTTP loopback 的交错单 fetch 样本为 identity **0.109 / 0.113 s**，LZ4 **0.262 / 0.261 s**，在该快链路上更慢。计时包括 HTTP、压缩和解压，排除 cloudpickle serialization/unpickle，不能当作完整 SDK 或 WAN 延迟。

这轮抓取关闭了自动解压：identity/LZ4 的 HTTP entity body 分别为 **50,332,080 / 42,363,548 B**，均无 Content-Encoding；这也不是物理 wire bytes。高熵 bytes 和已压缩 PNG 未达到节约阈值，返回 identity，但尝试压缩仍消耗 CPU 并暂时分配完整候选 body。二进制及 LZ4 均保留整块 pickle 的内存成本。远端是否收益取决于实际链路与两端资源，尚未验证。

显式 PNG 导出也需要单独衡量：首次 probe 总计 **6.595 s**，其中 await **3.624 s** 已含 encode **2.379 s** 和 helper 首次源码上传；同图 float fetch 加本地 save 约 **6.020 s**。这是不同阶段的有限样本，不能证明稳态 PNG 更快或更慢。PNG 文件约 **4.82 MB**，RPC 解码后 JSON payload 约 **6.42 MB**，同样不是已测 wire bytes；PNG 的 8-bit 输出契约也不能静默替代 float tensor。

## 本地图片与模型文件

本轮公共入口为 `await remote.files.upload("local.png")`，直接返回本地文件元数据，再把 `.native_name` 交给 `LoadImage`。它使用独立流式 I/O，上传、下载都单遍读取并做尾部 SHA256 验证后发布，不通过 Python 代码 artifact 或整文件 pickle/base64 staging。

默认 stat 不扫描大文件；`checksum=True` 才显式计算 SHA256。模型上传先列出并明确选择原生类别根，不能覆盖或制造同名 shadow。文件共享且持久于 session，GC/Client close 不删除；取消晚于提交点可能已留下完成文件。调用签名、持久性和边界集中在[文件 I/O](design/files.md)。

## 旧项目哪些机制值得保留

| 旧实现 | 已确认的边界 | 当前可借鉴点 |
| --- | --- | --- |
| 对象大小分流 | 压缩后小于 1 MiB 则 inline base64，大对象经临时文件传二进制；仍整块序列化 | 显式二进制数据面 |
| numpy | 顶层 ndarray 使用 pickle，可选 LZ4；不是 raw-array codec | 能力协商和压缩对照 |
| tensor / 普通对象 | 没有 tensor 专用 codec；通用 pickle，达到阈值再 gzip | 保留通用对象 fallback，先测 CPU/大小 |
| preview | 只取 batch[0]，float 转 uint8，最长边 1024 | 仅作为显式预览契约 |
| 文件 API | 实际实现过 upload/download/list，依赖旧环境与原生文件路由；不是通用续传或任意模型根上传 | 文件生命周期独立于对象引用 |

旧进程级 LRU 强持有对象、原地变更可能复用旧序列化，以及远端临时文件缺少对应清理的行为，不应迁入当前 ownership 设计。旧实现已有文件能力，也不能据此声称当前薄 daemon 自动拥有 ComfyUI 的所有文件路由。

## 最终远端验收还须补什么

待源码与接口冻结后，补入一份同环境、交错重复的 serial/overlap 真实 H3 记录，分别报告：

- 构建：SDK/daemon commit、wheel SHA256、实际导入源码 hashes、Comfy/模型 hashes、完整依赖、原生 flags；CPU/quota、GPU/driver、host、可取得的 immutable image digest。
- 计算：fresh process、setup、首张、变 seed 热计算、forward 次数和 GPU 完成屏障；完全相同输入的 cache hit 另列。
- 用户时间：await、fetch、save、单张 latency、持续 images/min；同时给含 setup/provenance 的总时间和稳态窗口。
- 数据面：实际 Content-Type/Content-Encoding/Content-Length，关闭自动解压抓取的编码 entity bytes、解码 payload bytes、两端 CPU 成本、结果值/shape/dtype 校验；说明实际 WAN/proxy 路径。
- 所选功能：binary identity 与协商 LZ4 的真实图片对照，压缩不可用的 fallback；正式 files API 的上传/读取/校验/取消/清理和文件持久性证据。

这些字段未齐前，不把本地 loopback、codec 理论盈亏点或 CPU recipe 调度结果写成远端吞吐改善。
