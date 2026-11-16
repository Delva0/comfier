# 管理候选的 H3 等待与性能

2026-11-16。本文记录独立 GPU 实测及另一名审阅者的离线复核。性能样本绑定 `f766394c…55fdb` wheel；`d9719429…c1d2c` 的普通磁盘发布与出图另见 [管理验收](management_acceptance.md)。它们不认证当前工作区中待定的输出 API。

完整数值和来源摘要见 [结构化记录](../artifacts/management/2026-11-16-acceptance.json)。实际 H3 入口为 [冻结脚本](../artifacts/management/2026-11-16-h3/minimax_h3_remote.py)，不是以后修改的同名工作区文件。该脚本使用已测候选的 `Call` / `.output()` 接口；同目录保留其 source helper。

## 可比较的条件

本次使用同一 RTX 4090、Torch `2.10.0+cu130`、ComfyUI `eee59185ff4f43ae2d9ad122b245619d27d9daad`，相同三模型 SHA、提示词、2048×2048、batch 1、6 Euler/simple steps、CFG 1、`--reserve-vram 4`，custom nodes 开启。实际 Loader/KSampler/VAE 为原生模块，H3 conditioning 来自原生 `comfy_extras/nodes_minimax_h3`。

无 observer 主跑为三种模式各用 fresh worker，先用 seed 501000 warmup，再分别真正生成 501001、501002；两个新 seed 的三路径解码像素精确一致。模式按块顺序执行，没有随机交叉重复，不能消除 CPU 调度、GPU clocks 和时间漂移。“冷”仅指 fresh worker、模型/CUDA 进程状态；模型已下载，未清 OS、driver 或 JIT cache。`f766` 管理 workspace 的 tmpfs 是旧磁盘失败后的明确 workaround，不能用于证明普通 60 GiB 磁盘够用。

## 计算、保存与网络分别计时

下表无 observer 的 compute-ready 不含 SaveImage、stat 或文件下载；total 包含这些阶段。每个样本均保留，不能挑最小值作为通用结论。

| 模式 | fresh worker compute-ready | seed 501001 热 compute-ready / total | seed 501002 热 compute-ready / total |
| --- | ---: | ---: | ---: |
| eager | 100.703 s | 6.572 / 10.654 s | 7.028 / 10.511 s |
| captured graph | 111.677 s | 5.112 / 13.951 s | 4.451 / 11.199 s |
| native prompt | 116.794 s | 4.712 / 9.451 s | 4.281 / 9.569 s |

原公共脚本连续三模式沿用同 seed、worker 和上游缓存，最初约 103 / 14 / 9 秒只证明真实出图与像素一致，不能作为 API 加速比。`captured_graph` 是显式工作流捕获，不是 CUDA Graph kernel capture；`native_api` 仍通过 Comani 的 NativeAdapter 和原生 PromptExecutor，没有旁路到独立官方 HTTP server。

独立的轻量 node profile 与无 observer 主跑分开。默认线程的冷 profile ready 为 114.921 s，其中 CLIPLoader 32.142 s、conditioning 48.091 s、UNETLoader 4.776 s、VAELoader 3.802 s、KSampler 20.183 s。它定位了本次百秒等待中的主要 CPU 工作；不能将另一轮 profile 强行拆成无 observer 首图账单，也不能由此认定历史约 150 秒的根因已完整闭合。节点时间包含原生物化、cast、offload、同步及 CPU 工作，不是纯 GPU kernel 时间。

## CPU 线程干预

该容器 CPU quota 约 30.6 核、affinity 255；pristine Torch 实际 intra/inter 线程为 255/128，并非 Comani 硬设。第一模型节点前显式设置并读回 32/32 后，两次独立无 observer 冷 compute-ready 为 **36.727 / 32.197 s**；另一次独立 profile 为 30.084 s，CLIPLoader 0.831 s、conditioning 9.483 s、UNETLoader 0.146 s、VAELoader 0.294 s。这是线程配置影响本次冷加载的重复干预证据。

32/32 的热新 seed 样本为 5.764 / 4.594 / 4.718 / 4.643 s，未证明稳定热吞吐提升，也未证明 32 是通用最优值。仅设置 `OMP_NUM_THREADS=MKL_NUM_THREADS=32` 实际得到的是 **32/128**，不能冒称复现了 32/32；已测 wheel 没有新增专用 worker 线程参数。interop 的显式设置要在并行工作开始前进行，不能在运行中偷偷重设。

## eager 与 capture 的差值

单组独立 observed 热新 seed 中，eager 与 capture 的 client-ready 差为 2.501317 s，可见节点 wall 差合计 2.502014 s：

| eager 减 capture | 节点 wall 差 |
| --- | ---: |
| H3 conditioning（eager 实际重跑，capture 复用缓存） | +1.516939 s |
| ConditioningZeroOut | +0.014985 s |
| KSampler | +0.956446 s |
| VAEDecode | +0.013644 s |

本组差值主要已在节点执行内闭合，不能写成少几次 RPC 节省了 2.5 秒。KSampler 差还没有拆成 kernel、offload、CPU 和调度的单因素。observer 不加节点 GPU barrier、NVML polling 或 stack sampling，但仍有测量扰动，且这里只各有一个样本。没有同条件的独立官方 HTTP server 对照，因此没有量出纯 Comani 固定开销。

固定模型、提示词与尺寸时，可显式复用已准备的模型和 conditioning refs，或使用显式 graph；输入变化时重新准备并最终 release。旧 host 的 refs 试验仍归属于 [历史性能记录](performance.md)，不能作为当前 wheel 新测结果，也不足以支持新增调度器或 GPU streams。

## PNG 与 WAN 等待

只需 8-bit PNG 时，本轮脚本实际调用远端 SaveImage，再从 metadata 取得文件，通过 files 下载。无 observer 保存调用约 1.19–1.41 s，独立 observed 原生 SaveImage 约 0.899–0.939 s，二者边界不同。

同一个 4,870,305 B PNG 的两次 WAN download 总计 **7.167 / 4.585 s**，每次包含 window/start/GET/status 四个 HTTP 请求。GET headers 为 0.774 / 0.508 s，headers 后应用读取 span 为 5.184 / 3.154 s；该 span 仍包含客户端消费和写文件。相同 host loopback download 为 0.0348 s、stat 0.0054 s。这表明此次 WAN 路径存在明显等待，但不是 packet capture、物理 wire 吞吐或公网单一因素的证明。

需要精确 float Tensor 时仍应 fetch；没有匹配输出的 float/PNG 对照就不能承诺 PNG 加速倍数。现有 transfer admission、epoch、恢复、SHA 和最终 status 都有正确性职责，减少往返仍需独立设计与恢复验收。本轮没有实现这项优化。

历史 LZ4 和 max-2 流水实验仍保留原结果：没有稳定端到端加速证据。本次性能分析也没有提供可靠的 GPU 利用率或峰值 VRAM，不能据此硬设 highvram、compile、第二套 VRAM 池或多执行器。
