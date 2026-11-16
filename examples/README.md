# 工作流示例

这些脚本用公开 Comani API 准备可选依赖、运行原生节点并下载结果。计算和模型留在 daemon；`--output-dir` 是客户端目录，每次运行建立一个新的 `run-*` 子目录，保存输出与 `profile.json`。运行记录中的软件版本、模型 revision/SHA、参数与阶段耗时应一起保留。

## 先准备 daemon 和计算环境

客户端与服务器管理环境先安装同一份 Comani wheel，并按[项目入门](../README.md#开始使用)启动 daemon。机器租用、SSH/HTTPS 入口、GPU 驱动和适合硬件的 Torch/CUDA 由部署者选择。客户端只需要 Comani；不需要装 Torch 或复制服务器模型。

```sh
export COMANI_URL='http://127.0.0.1:8190'  # 或部署者提供的 HTTPS / SSH 转发入口
export COMANI_TOKEN='...'                 # 仅在入口要求鉴权时设置
```

下面十个脚本的普通运行都复用已配置的当前 ComfyUI；`Client` 连接不会安装它。空白服务器可先按[环境准备指南](../docs/design/development.md#空白-cpu-环境)创建环境，再用现有公开 API 选择 GPU 的解释器、Torch 包与 ComfyUI。不要把 CPU 指南中的 Torch 配置直接当作视频 GPU 配置。完整的显式准备调用见 [minimax_h3_remote.py 的 `prepare`](minimax_h3_remote.py)，其中环境创建、`comfyui.configure/prepare_tools/install/prepare_runtime` 与所选包安装都在计算前完成。它是 API 用法参考，下面的工作流各自使用自己的权重。

普通运行检查节点能力，不要求历史软件 commit。仅在明确需要更新时选择版本或 commit。安装或更新插件要等已有下载和计算结束，再停止计算、安装缺失包、启动并检查节点；不要在大模型下载中间重启环境。

## 当前准备范围

“内置”指该工作流脚本已有相应参数。模型下载均由 daemon 直接取得 Hugging Face 文件，并记录解析后的 revision/SHA；不会先把大模型下载到客户端再上传。

| 脚本 | ComfyUI 以外的节点包 | 内置准备参数 | 仍需事先完成 |
| --- | --- | --- | --- |
| [qwen_click_mask.py](qwen_click_mask.py) | ComfyUI-AusBoss | `--install-plugin --download-models`，4 个权重 | 当前 Qwen 2.1 / SAM 3.1 原生节点与兼容硬件 |
| [qwen_cut_out_remove.py](qwen_cut_out_remove.py) | ComfyUI-AusBoss | 同上，5 个权重 | 同上；按素材验收透明度和背景修补 |
| [qwen_tiled_upscale.py](qwen_tiled_upscale.py) | ComfyUI-AusBoss | 同上，5 个权重；`--no-turbo` 只需 4 个 | 当前 Qwen 2.1 原生节点与兼容硬件 |
| [qwen_outfit_swap.py](qwen_outfit_swap.py) | ComfyUI-AusBoss | 同上，4 个权重 | 当前 Qwen 2.1 原生节点；自动描述需要 `TextGenerate` |
| [h3_seed_hunter.py](h3_seed_hunter.py) | 所用节点均来自当前原生 H3 后端 | `--download-models`，6 个权重 | 支持所选 INT8 / NVFP4 编码器的 GPU 和运行环境 |
| [moss_bbox.py](moss_bbox.py) | 所用节点均来自当前原生 MOSS 后端 | `--download-models`，默认 3 个；`--hires` 加第 4 个 LoRA | 当前 MOSS 原生节点 |
| [ideogram4.py](ideogram4.py) | KJNodes、Detail-Daemon | `--install-plugins --download-models`，3 个权重 | 当前 Ideogram4 原生节点与兼容硬件 |
| [h3_longforge.py](h3_longforge.py) | 官方 LongForge 附件中的 NodePack | `--install-plugin` 让 daemon 按官方 URL/SHA 安装并准备 filelock/imageio-ffmpeg；`--download-models` 下载 4 个默认权重 | 官方 archive 的实际 daemon 获取尚未验证；当前 H3 后端与兼容硬件 |
| [score_to_screen.py](score_to_screen.py) | 官方附件中的 ABCScoreIO、PromptSplitter、ABCTiming、ABCInstrumentalize | `--install-plugins` 让 daemon 从同一官方 archive 安装缺失包；`--prepare-llama` 准备原工具；`--download-models` 下载 6 个默认权重 | 官方 archive 的实际 daemon 获取尚未验证；Python 3.12 和当前 FastH3 后端与兼容硬件 |
| [h3_perfect_looper.py](h3_perfect_looper.py) | CoolB Nodes、H3-Motion-Context-MultiRef、KJNodes | `--install-plugins --download-models`，5 个权重；`--download-lora` 可只准备原 Turbo-v4 | 当前 H3 后端与兼容硬件 |

准备、执行和结果下载均已写入各示例入口；LongForge/Score 的官方 archive 直下仍是未验证项，不能声称十例均已从零跑通。PerfectLooper 的新增准备路径目前只完成本地检查，不自动继承旧执行路径的 GPU 验收。脚本不会创建或租用机器，也不会自动替调用者选择 Python、Torch/CUDA 或 GPU。

LongForge、Score 是原作者官方公开附件中的节点包；其公开资源应由 daemon 获取，不要求调用者先保存到个人目录再上传。原包未随仓库分发；不要将第三方代码、权重或 `.local` 验收目录提交到 Git。PerfectLooper 的公开包为 [CoolB Nodes](https://github.com/CoolBreeze164/ComfyUI-CoolB-Nodes)、[H3 Motion Context](https://github.com/seitanism/ComfyUI-H3-Motion-Context-MultiRef) 和 [KJNodes](https://github.com/kijai/ComfyUI-KJNodes)；复用已安装的匹配节点能力，记录实际安装身份。

## 准备、执行和取回

例如已配置的 Qwen 实例第一次运行 Click to Mask：

```sh
python examples/qwen_click_mask.py --install-plugin --download-models \
  --input desk.png --point 400 300 \
  --prompt 'Replace the cup with a glass of orange juice.'
```

后续可去掉两个准备参数，复用已验证的模型和插件。`--install-plugin` 仅在发现缺失插件时停止计算并安装；没有缺失时不为安装而重启。`--download-models` 对相同目标使用 `reuse=True`，仍核验模型 SHA。Qwen Tiled 的默认分支包含 Viggle Turbo；`--no-turbo --steps 25` 选择源工作流的慢分支。

后三例的显式准备调用如下。LongForge/Score 的官方附件尚无 daemon 直下验证，受限环境不得把以下命令当作已通过的首次部署：

```sh
python examples/h3_longforge.py --install-plugin --download-models

python examples/score_to_screen.py --install-plugins --prepare-llama --download-models \
  --image adult.png --audio song.wav --abc song.abc

python examples/h3_perfect_looper.py --install-plugins --download-models
```

LongForge/Score 脚本中的 `NODE_ARCHIVE` 记录已保存官方 metadata 的来源，未执行受限资源请求：

| 来源 | 固定附件身份 | 字节数 / SHA256 | 节点子目录 |
| --- | --- | --- | --- |
| AIBOB1199 · [LongForge v7.8](https://civitai.com/models/2926029) | version `3467080` / file `3370455` | `649345` / `5754f4594ed185577da67e1b4015139793bbfdbbeca0ef33b0a378b8185ccc0d` | `ComfyUI-H3-LongForge-NodePack/` |
| koyan0418439 · [Score to Screen v9.0](https://civitai.com/models/2975051) | version `3470191` / file `3377291` | `2056796` / `c6f55583038e259c441cebe8a0e31e9cfc727f9f4990e1dc65ed83488dad4b49` | `Score2Screen_v9.0/custom_nodes/` 下表列四包 |

Score 四包为 `ComfyUI-ABCScoreIO`、`ComfyUI-PromptSplitter`、`ComfyUI-ABCTiming`、`ComfyUI-ABCInstrumentalize`。官方说明未给出这些原包的独立 GitHub/registry 发布。`plugins.install` 接 registry ID，`install_git` 接 Git 仓库；例子用 `plugins.install_archive(NODE_ARCHIVE)` 表达官方 ZIP 来源，由 daemon 获取、校验 SHA、安装选定包目录并正常处理包内依赖。同一个 ZIP 的多个包共用一次获取，例子只提交缺失包；archive 不经过客户端。LongForge 未在 pyproject 声明的 filelock/imageio-ffmpeg 由例子显式安装，不从历史验收环境推断依赖已齐。`custom_nodes` 继续属于软件路径，不开放为数据上传根。官方 archive 的实际 daemon 下载与安装尚待验证，不以本地已有附件上传代替通过。

后三例分别并发准备自己需要的独立模型文件；所有已开始的准备均结束并且全部成功后才加载模型。Score 先检查 Python 3.12 和原 `llama-server --version`，缺少工具时仅 `--prepare-llama` 允许原作者代码选择当前平台兼容的官方 release；不硬编码 b12436 或软件 commit。普通运行不悄悄进入首次工具下载。LongForge/Score 的 `--download-models` 对应脚本所列默认权重组，自定义 `--model` 等参数应先安装选定权重并省略此准备参数。

每个脚本的执行路径都可直接阅读：`comfyui.start()` 检查原生节点 → `files.upload()` 传输入 → `node()` 连接原生节点 → 输出节点生成文件 → `Call.file()` / `files.download()` 取回。Score 使用显式 `graph()` / `submit()` 保留原节点的动态场景模板与隐藏图链接；将它改成普通 eager 链会改变其行为。LongForge 刻意关闭第一次 Client 后重新连接同一项目，证明原生持久化 take 可供下一场景续用。

文件取回使用同一组公开 API，无需知道服务器输出绝对路径：

```python
saved = comani.node("SaveImage", images=image,
                    filename_prefix="comani/example", return_call=True)
file = await saved.file()
await comani.files.download(file, output_dir / "result.png")
```

视频对应 `CreateVideo` / `SaveVideo` 后采用相同取回步骤。Score 的 `submit` 包含多个节点时使用 `await run.file(node_id=saved.id)`。保存的远端结果不会因 Client 退出而删除。上传输入的清理由各脚本显式负责；Score 当前保留上传的图片、音频与 ABC，因为原生运行记录和场景图仍会引用它们。

H3 Seed Hunter 默认生成三条不同 seed 的预览，再让调用者选择一个远端 AV latent 精修；`--choose 2 3` 只生成并精修已知的两个候选。Qwen 的 `--count` 增加不同 seed 的产品，Click/CutOut 的 `--repeat` 只作同 seed 缓存观察，不能计作新增独立产品。PerfectLooper 的 `seam-check-x2.mp4` 也是同一条真实生成闭环的两次播放，不能计作第二条生成结果。

## Score 的 CPU LLM 线程

`--llm-threads N` 把 `-t N -tb N` 写入原生 `S2SLLMSettings.extra_args`，分别限制 llama.cpp 的推理和 batch 线程。省略参数保持原节点默认；不改变 ComfyUI、Torch 或 H3 的全局线程配置。

按服务器的进程 affinity 和 cgroup 配额选择 N，不能按宿主机 CPU 总数直接设置。一次实际环境的配额为 11.52 核，原 CPU LLM 默认开启 48 个推理线程；该环境下一次可显式试 `--llm-threads 11`，并比较同模型同输入的 LLM 阶段耗时。这是待验证的调整，不是通用默认，也尚无加速收益结论。没有 Comani 内置 CPU 配额查询 API；配额应由部署者的现有系统信息确认。

```sh
python examples/score_to_screen.py --image adult.png --audio song.wav \
  --abc song.abc --llm-threads 11
```

该命令只适用于已完成表中 Score 准备的实例。不要在当前 LLM/采样期间更新脚本部署、改节点包或启动另一轮任务。

`--llm-device cuda` 保留同一 GGUF/F16 视觉权重，只选择原节点的 `llama_build=cuda` 与 `gpu_layers=all`。工具预检必须通过 `--list-devices` 实际枚举 CUDA 设备，CPU 默认不变。原节点在 LLM 前释放 ComfyUI 模型显存，并在 `unload_after=True` 时关闭本地 server，随后才进入 H3。官方 llama.cpp 提供 [Ubuntu CUDA 构建](https://github.com/ggml-org/llama.cpp/releases)，但所选 release 的驱动、运行库和 GPU 兼容性仍需在目标实例确认；新增参数目前没有 GPU 成功或性能收益证据。下一步应重放原失败的 PromptSplitter 输入做同参 GPU 对照，不通过延长 600 秒请求掩盖 CPU 超时。

## 复现与验收边界

截至 2026-11-16 的独立验收记录：

| 工作流 | 已有真实后端证据 | 尚不能宣称 |
| --- | --- | --- |
| Click / Outfit / Tiled | 严格图片分别 4 / 4 / 1；Tiled 已实际分成 2 tile | 更多 seed、重复缓存执行或未检查输出的质量通过 |
| CutOut | 调整参数后 1 张背景达到严格验收 | 原默认参数或透明前景已无瑕疵；材质相关 alpha 调整仍需检查 |
| MOSS | 严格图片 5 张 | 未运行配置的质量通过 |
| Ideogram4 | 真采样输出可解码 | 细分植物品种符合提示；该偏差使其未计入严格图片数 |
| H3 Seed Hunter | 原 107 帧路线视频 3 条，345 帧扩展 1 条，技术与画面已检查 | 音频听感已验收；扩展等同原 GUI 基线 |
| LongForge | 小变体 2 × 2 秒，跨 Client 持久化续跑并导出 1 条，技术与画面已检查 | 原 GUI 的较长尺寸/时长基线，或音频听感已验收 |
| Score | 原 4B GGUF/F16 视觉 CPU LLM 已实际运行；第二次基线在 684.486 秒以原请求 600 秒超时结束，尚未进入 FastH3 | 已得到成片或工作流通过；线程调整收益 |
| PerfectLooper | 42 类实际节点 schema 与 CPU 视频/音频拼接几何已核对 | 模型推理、真实闭环观感或听感通过 |

严格图片合计 15 张；技术与画面视频合计 5 条，均未完成音频听感验收。H3 的原始失败、调整参数和扩展分支分别记录，不以重复 seed 增加计数。每个 `profile.json` 是这次执行的记录，历史成绩不会自动认证修改后的脚本。

工作流链接是来源标识，不能把浏览器/API 受限资源当作已通过 Comani 远端下载验证。这里的远端直下证据来自各已执行的 Hugging Face 模型下载；用户本地取得后上传的文件须单独标记。模型、LoRA、节点包和输出均继续遵守各自条款。
