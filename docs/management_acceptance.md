# 管理架构与恢复验收

历史快照（截至 `3d07322`，管理验收发布为 `11318c6`）：文中“当前”“待定”等表述均指该历史快照；现行 node 语义见 [SDK](design/sdk.md)。

2026-11-16，日期按 Asia/Shanghai。本文按实际测试的三个候选阶段记录管理架构、恢复、文件与模型发布、示例、GPU/WAN 和性能证据。**稳定管理、传输、stage 与 cleanup 范围已正常发布为 [11318c6](https://github.com/Delva0/comani/commit/11318c6cf9be761d67088fa83f33b140bc849fef)，并于 2026-11-16T03:20:25.720347Z 核验远端与本地 HEAD 一致；39 个产品模块匹配 cleanup 候选。** 未定输出 API 的未验收改动已独立归档，不属于稳定产品或这些候选。精确 SHA、全部性能样本与逐项需求归属见 [结构化验收记录](../artifacts/management/2026-11-16-acceptance.json)。

本轮范围为 R01–R25，不包含旧的 39 项可选 CLI 扩展，也不建立跨 daemon 崩溃的持久 exactly-once 或传输历史数据库。已测候选保留 `Call` / `.output()` 接口；按实际执行结果塑形的 node 返回提案仍待定，不借用本报告宣称实现或验收。

## 已测候选

| 候选 | 身份与证据 |
| --- | --- |
| 管理基线 | wheel SHA256 `f766394cb4598d76046f1dae8d55857076feb4013d60964cbf8821a4ecd55fdb`；本地 checkpoint `a9c3bf9999ee38e3c2d586451b04650749044df6`。独立 409 CPU 通过、2 GPU deselected；真实部署、provider、Manager/Git 与独立 7 组政策验证。 |
| 暂存发布修复 | wheel SHA256 `d97194297dbf0e3e4c5ba75cce9d962a1377b19b67c0cd580ffcef8c5dec1d2c`；完整冻结 source manifest `d7b9ed838534351113a32e1d3333c8f8e4a11c69e6f302ace4e38e2bb46409e8`。独立 431 CPU 通过、2 GPU deselected，ruff 通过。 |
| 缺失输出 cleanup 修复 | wheel SHA256 `1b1701ef4937647a32def481f73c7117afe334a7fed0fe202129a02577d599f0`；以 `d971` 为基线，仅增加 `service._port_ref` 的异常安全清理和独立回归测试。独立 441 CPU 通过、2 GPU deselected、0 skip，相关 78 项和真实 native 26 项检查通过；改动文件 Ruff 通过。 |

第二个候选相对基线只改 `daemon/files.py` 与 `daemon/management/models.py` 两个产品文件，并新增 22 个 stage publication 测试。独立 QA 核对源码、archive、Torch-free 管理安装、wheel-test 安装和实际 CPU worker 的 39 个 Python 模块；原离线图捕获与 exact-wheel 测试保留在完整 CPU suite 内。README 与 wheel METADATA 正文逐字匹配。

第三个候选的产品相对 `d971` 仅变动 `daemon/service.py`，新增 10 项独立 cleanup 回归；当前源码的 39 个产品模块与该候选的独立源、wheel 和隔离安装逐字节相同。README、METADATA 和 entry points 与 `d971` 相同。441 项完整 CPU 使用隔离安装的 wheel 和 `python -I`，没有用未选 API WIP 代替该候选；此候选没有重新运行 GPU、WAN、模型推理或性能测量。

202 个文件的 stage manifest 是该次测试的冻结身份，包含其测试与文档范围；它不是 cleanup 后的完整交付树或本文档更新的 hash。cleanup 的 39 模块规范 manifest SHA256 另记为 `912ac792713a7db18aa9d393497b2c89017fb7d54eae20a6f71e94f7ed7aebc6`。外网 provider、Manager、Git、bootstrap 和政策结果按基线归属，相关未改模块的 hash 已核对，没有把它们改记为在后续 wheel 下全部重新执行。更早的独立恢复探针同样保留 source-stage 身份。

## 缺失输出的异常安全清理

独立真实 native 对照复现了 producer 未发布被引用槽的情况：旧版消费者保留原 `IndexError`，但后台 admission task 还有未取回异常，另一个正常输入的 lease 在会话关闭后仍残留。修复仅让内部清理跳过不存在或未发布的引用，继续释放其余依赖；消费者等待缺失输出仍报原错误，没有为 native 补槽，也没有增加声明数或实际输出数预检。

候选的 26 项真实 native 检查覆盖 ready/pending 缺槽、正常/失败 producer、graph output、执行中关闭、提前 release/GC 和另一会话可读。所有相关 reservation、lease、Call/object/session 资源归零，未再观察到未取回后台异常。独立 gate SHA256 为 `a9f3fc617e0f5754b2396ea39e9699987c40f394249d1831510a62716fe46700`；这是现有 Call 接口的资源清理修复，不是新返回 API 的验收。

## 管理、部署与恢复

独立验收实际启动生成的 `comani daemon` 与 `python -m comani daemon`，核对 console 入口为 `comani.cli:main`。依赖方向为 root `__main__ → cli → daemon`；daemon 没有反向 CLI import。旧 `_runtime`、standalone daemon module 与被移除 helper 入口不在新 wheel 中，也不保留 redirect shim。

Torch-free 管理环境在 ComfyUI 未安装、停止或启动失败时仍可用。独立目标 Python 只通过隔离的 package bridge 获取当前 Comani 代码，未暴露管理环境 site-packages；显式 `prepare_runtime` 的同版本替换核对实际字节，其他 distribution 版本集合未变。真实 public HF 固定 commit 小文件/manifest/snapshot、Civitai 具体 artifact metadata、官方 Manager 生命周期和 Git SHA/dirty/partial 情况均保留独立证据；受限账号和大 LFS/Xet 不由这些事实推定。

7 组政策专项独立观察了默认 stop/restart 排空、显式 `wait=False` 中断实际计算和受管 pip、partial receipt、新 Client 重新附着，以及 bash 在重启期间继续运行。pip fixture 的副作用确实保留；没有安装回滚或重启必成功的承诺。

早期独立恢复包括 lost reply 去重、同 session/epoch 恢复、keeper、offline close/release、缩短 grace 的 expiry/session isolation、容量背压、Operation 限额，以及文件半开取消缺陷修复后的竞争复验和实际 62 秒分区。默认 grace 为可配置一小时，但没有把缩短 grace 的探针写成等待满一小时。后续 WAN 的最终归属见下节及结构化记录。

## 暂存发布与真实磁盘

同文件系统下，模型获取完成并核验的私有独占 stage 直接以 create-only hard link 发布，随后移除 stage 名字。它消除发布阶段第二份整文件分配；内容获取与核验仍有真实 I/O。

独立本地验收在真实 16 MiB tmpfs 中放置 8 MiB stage，剩余空间仅 1 MiB：公开 HTTP 单文件下载和 `snapshot(publish="files")` 均成功，目标保持同 inode 与 allocation，发布阶段 payload 读取与 Upload 写入均为零。实际 EXDEV 使用有界复制并独立校验 SHA，真实目标空间不足时拒绝、源保留；损坏复制被拒绝，取消等待磁盘任务排空，原子发布已经完成时保留真实 metadata。实际原生 worker 的 filename-list cache 在发布后失效并读到新模型。

远端普通 60 GiB 磁盘的 `d971` 实测重新获取 Civitai model/version/file **3045867 / 3462708 / 3365259**：`20,979,337,386 B`，SHA256 `4647bdf553a32aa4ae55d9eb28322be42d32611d333c19a097d2845ae8a9b7a8`。stage 与最终目标为同一 inode，观察到最低剩余 **17,420,972,032 B**，小于再分配一份 payload 所需空间。此项已由最终独立报告封存通过，与先前 `f766` 使用 tmpfs 绕过双份磁盘的运行分开归属。

信任边界仍按文件权限解释。独立探针保留了同 daemon UID 在相同 revision 下改写 stage、可能留下旧 SHA metadata 的反例；本轮没有声称隔离这种恶意 writer。跨文件系统仍需完整目标分配，HF acquisition cache 峰值没有改变。实际本地文件系统覆盖 Linux/WSL，未认证 Windows/NTFS。

## 实际 H3 与 GPU/WAN

独立远端报告的 SHA256 为 `0f528a12f60a593d25fcc2cf74eaf8685ba1d746e99c0492e0ee7ef9715215fa`，结论为带明确限制的 scoped pass，未宣称整体项目或新 SDK 已完成。

本节 GPU/WAN 结果分别归属 `f766` 与 `d971`，没有重新贴成 `1b1701ef` cleanup 候选的远端测试结果。

`d971` 在同一 RTX 4090、Torch `2.10.0+cu130`、ComfyUI `eee59185ff4f43ae2d9ad122b245619d27d9daad` 上实际执行了 [冻结的 H3 主脚本](../artifacts/management/2026-11-16-h3/minimax_h3_remote.py)，SHA256 `63000339532ef98ccaac5cf78e88497fa1a7224810d29bd2a9ce1fa2f4543256`；其 [source helper](../artifacts/management/2026-11-16-h3/minimax_h3_source_ops.py) 同目录保留。该副本采用候选原有 Call 接口，可用于核对本次实际调用形态，不指向未来改变后的工作区示例。

seed 426190、2048×2048、6 steps 的 eager、captured graph 和 native prompt 三路径均实际生成 PNG，三者解码像素精确一致，与 `f766` 同 seed 也一致。模型包括上述 INT8 diffusion、15,687,142,551 B encoder 与 1,168,505,312 B VAE，完整固定来源与摘要见结构化记录。PNG 一致性不替代 float Tensor 精确 fetch 验证。

![d971 候选实际生成的 H3 2048×2048 PNG](../artifacts/management/2026-11-16-h3/eager.png)

这张实际输出 PNG 的文件 SHA256 为 `c837ea2c21879784695d206fd9f4b08fb21fd066ae86a589e4574317aaffc417`。图片 metadata 仅保留本次原生 prompt，没有复制 endpoint、凭据或 raw logs。

`f766` 的 GPU 断言运行为 **2 passed、1 skipped，113.37 s**：通过的是原真实 CUDA node→Python→node parity，以及独立适配的 H3 公共 pipeline eager/native 精确 float parity（CPU float32，`[1,2048,2048,3]`，最大绝对误差 0）。原通用 CheckpointLoader 用例因缺少兼容的单文件 checkpoint fixture 跳过，分离的 H3 权重没有被冒充为该 fixture，不能写成全部原 GPU tests 通过。此组断言未在 `d971` 上重新运行；`d971` 新实测范围为管理/目标/worker 的 39 模块身份、普通磁盘 21 GB 发布、三模式 PNG 和 stop 后管理存活。

WAN 故障同样归属 `f766`：受控 forwarding proxy 在真实远程 HTTPS 路径断开 socket 转发 2 秒，管理 Operation 与实际 source Python 计算的副作用均只执行一次，新 Client 附着原 Operation；它不是全 host 网卡断网或采样中 GPU kernel 故障。8 MiB 文件下载观察到非零 Range/206 恢复且最终 SHA 精确相等。上传确认同一 transfer identity、从服务器确认的 **offset=0** 重传后 SHA 相等，**没有证明非零前缀 WAN 上传续传**。真正 restart 后旧 ref 失效，三模型内容核验后均 `reused=True`。这些故障没有全部在 `d971` 重跑。

## 性能结论与交付范围

[本轮性能审阅](management_performance.md) 保留无 observer 冷/热样本和单独诊断边界。CPU quota 约 30.6 核时，Torch 实际默认 255/128 线程；加载前显式 32/32 的两次冷 ready 为 36.727 / 32.197 s，默认三模式为约 101–117 s，热吞吐没有稳定改善证明。单组 observed eager/capture 的 2.501 s 差值与节点 wall 差 2.502 s 闭合，不能写成纯 RPC 税。OMP/MKL 环境仅得到 32/128，不能冒称复现 32/32。历史性能、图像和失败记录均保留原身份。

| 需求 | 已有证据与范围 |
| --- | --- |
| R01–R04 | 独立管理、单 endpoint、lazy attach、显式生命周期；public 部署、完整 CPU 与政策专项分别归属。 |
| R05–R10 | 同代恢复、调用去重、keeper/grace、容量与 Operations；source-stage 独立故障证据、当前 CPU 和最终 WAN 不混为一次运行。 |
| R11–R13 | 独立 bash、目标环境/core/readback、官方 Manager/Git 与私有多 root；partial 事实不冒称回滚。 |
| R14–R18 | 原生 roots/create-only、provider 选择与校验、HF snapshots、stage 发布和客户端文件恢复；实际大文件、EXDEV、权限与账号限制分别说明。 |
| R19–R21 | 原生计算/metadata、必要 CLI 和重写示例；cleanup 候选有独立 CPU/native 清理回归，H3 冻结脚本的实际出图仍归属原 GPU 候选，待定 node 返回 API 排除。 |
| R22–R23 | 管理范围的聚合审核、文档/身份、精确提交与远端发布核验已完成；未定 API 和整体目标继续开放。 |
| R24–R25 | 管理/目标解释器隔离、package-only bridge、唯一公共入口与依赖方向已实际核对。 |

官方查询于 **2026-11-15 17:08:56.791976 UTC** 确认本轮实例销毁且列表为空；本地租期 watchdog 也已退出，本地 stage QA 自有 daemon 为零。创建到官方销毁确证的时长上界为 90 分 49.88 秒。按当时 **0.473333 USD/h** 报价计算的算力时长上界估算为 **0.716558 USD**，这不是总费用或实际账单；另有每供应商 reported GB **0.0286458 USD** 的入站报价、已知 provider payload 79,793,660,021 B，其他流量、重试及计费单位/取整不能由此推定。没有取得实际 invoice，也不声称最低小时价等于最低总价。

上述稳定管理范围已完成独立终审、交付冻结、精确提交、正常推送与远端 commit 核验；本次发布状态补记引用的是已发布的 `11318c6`。API 研究与原 WIP 保持归档隔离，最终设计、实现和独立验收继续开放；R22/R23 的管理发布子项完成，不代表未定 API 或整体目标完成。
