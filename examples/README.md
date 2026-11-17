# Python 示例

从仓库根目录运行下面的命令；各脚本的完整参数见 `python examples/<脚本>.py --help`。先用小例子了解调用方式，再按任务选择图像或视频工作流。

## 连接已配置的 daemon

客户端安装 Comfier，daemon 的计算环境安装所需 ComfyUI、Torch、节点包和模型。客户端无需安装 Torch；性能基准另有说明。启动和首次环境准备见[运行指南](../docs/design/development.md)，GPU 的 Python、Torch/CUDA 与硬件由调用者选择。

```sh
export COMFIER_URL='http://127.0.0.1:8190'
# 入口需要鉴权时，通过 COMFIER_TOKEN 提供凭证。
```

`Client` 连接只握手；脚本会显式启动计算。输入路径和输出路径属于客户端，计算与模型留在 daemon。模型准备由 daemon 直接下载，无需先下载到客户端再上传。

## 基本调用与组合

| 想做什么 | 最少命令 | 前提与产物 |
| --- | --- | --- |
| 上传图片，串联 eager 节点与少量 Python | `python examples/basic.py --input photo.png` · [脚本](basic.py) | 已配置的 ComfyUI，无模型；下载 `artifacts/basic.png` |
| 把 graph 的导出继续接到 eager 节点 | `python examples/graph_to_eager.py` · [脚本](graph_to_eager.py) | 无模型；下载 `artifacts/graph-to-eager.png` |
| 用已有 checkpoint 构建完整采样图 | `python examples/local_checkpoint.py --checkpoint my-model.safetensors` · [脚本](local_checkpoint.py) | 兼容的远端 checkpoint；下载 `artifacts/checkpoint.png`。可用 `--upload-checkpoint` 显式上传本地权重 |
| 显式准备环境和模型，运行 H3 原生图 | `python examples/minimax_h3_remote.py --output artifacts/h3.png` · [脚本](minimax_h3_remote.py) | H3 GPU 环境与三个权重；下载 PNG。环境准备参数见[指南](../docs/design/development.md#h3-完整脚本) |
| 把同一源文件里的 Python 函数发到远端计算 | `python examples/minimax_h3_source_remote.py --output artifacts/h3-source.png` · [脚本](minimax_h3_source_remote.py) | 与 H3 示例相同；处理后下载 PNG |

普通 Python 变量保存远端输出，后续节点可以直接使用它。只有需要本地值时才 `fetch()`；最终图片或视频通常在远端保存，再用文件 API 下载：

```python
import comfier as cf

saved = cf.node("SaveImage", images=image,
                    filename_prefix="comfier/example", return_call=True)
await cf.files.download(await saved.file(), "result.png")
```

`graph()` 用于需要整体提交或原生 lazy/dynamic 行为的部分；导出可接回 eager 下游。`submit(..., return_call=True)` 可保留整图的文件和节点观察入口，示例见 checkpoint。调用、引用生命周期和 graph 细节见 [SDK](../docs/design/sdk.md)。

## 图像工作流

下面的命令复用已准备的实例；首次安装节点包或下载默认权重时，加上后文对应的准备参数。本节图像脚本每次在 `--output-dir` 下新建 `run-*` 目录，保存输出与 `profile.json`。

| 任务 | 最少命令 | 产物与来源 |
| --- | --- | --- |
| 点选物体后局部重绘 | `python examples/qwen_click_mask.py --input desk.png --point 400 300 --prompt 'Replace the cup with a glass of orange juice.' --editable` | [Click to Mask](qwen_click_mask.py)：扁平预览；可选 `--editable` 交接独立 RGBA patch、两层 ORA、两种 mask 与源版本/几何 manifest；[改稿交付](../docs/editable-workflows.md)；[AusBoss 原工作流](https://civitai.com/models/3000390) |
| 抠出物体并修补原位置 | `python examples/qwen_cut_out_remove.py --input desk.png --object 'the mug'` | [Cut Out & Remove](qwen_cut_out_remove.py)：RGBA 前景与修补后背景；[AusBoss 原工作流](https://civitai.com/models/2987326)。透明边缘需按素材调整 alpha 参数 |
| 分块修复并拼回大图 | `python examples/qwen_tiled_upscale.py --input small-photo.png` | [Tiled Upscale](qwen_tiled_upscale.py)：放大修复 PNG；[AusBoss 原工作流](https://civitai.com/models/2988629)。`--no-turbo --steps 25` 选择慢分支 |
| 从服装图描述衣物并换装 | `python examples/qwen_outfit_swap.py --person adult.png --garment outfit.png` | [Outfit Swap](qwen_outfit_swap.py)：换装 PNG；[AusBoss 原工作流](https://civitai.com/models/2983126)。`--description` 可提供人工描述 |
| 用像素框安排角色位置 | `python examples/moss_bbox.py` | [MOSS BBOX](moss_bbox.py)：布局 JSON 与 PNG；[HissingCat 原工作流](https://civitai.com/models/3005854)。`--boxes` 接自定义框，`--hires` 开第二阶段 |
| 保留定制采样器的花卉生图 | `python examples/ideogram4.py` | [Ideogram4](ideogram4.py)：PNG；[原工作流](https://civitai.com/models/2674413)。权重采用 Ideogram Non-Commercial Model Agreement |

`--count` 用不同 seed 生成多个候选。固定图片、选择、模型与 prompt 的准备放在 seed 循环之前；改变输入后重新准备。Click/CutOut 的 `--repeat` 重复同一 seed，适合观察缓存行为。

## 视频工作流

| 任务 | 最少命令 | 产物与来源 |
| --- | --- | --- |
| 先看低成本候选，再精修选中的 AV latent | `python examples/h3_seed_hunter.py --prompt 'An adult walks through a sunlit garden.'` | [Seed Hunter](h3_seed_hunter.py)：三个预览和选中候选的精修 MP4；[foxydits 原工作流](https://civitai.com/models/2881362)。`--choose 2 3` 直接指定候选 |
| 保存场景，续写或替换中段后导出 | `python examples/h3_longforge.py` | [LongForge](h3_longforge.py)：`film.mp4`、场景/导出记录，远端保留 takes；`--project NAME --inspect` 检查工程，`--edit edit.json` 明确替换与 trim；[改稿交付](../docs/editable-workflows.md)；[AIBOB1199 官方来源](https://civitai.com/models/2926029)。默认是两段各 2 秒的小尺寸配置 |

Seed Hunter 和 LongForge 每次创建新的 `run-*` 输出目录。Seed Hunter 在候选之间复用固定条件；选定后，同一 AV latent 继续进入精修。LongForge 通过原生 `GENERATE NEXT` / `EXPORT FILM` 动作继续项目，重连时重新建立当前 Client 的模型引用。

Click 的 ORA 保存生成所见的源版本；已有后改底图时，应审阅后单独采用 patch，避免旧背景覆盖新笔触。LongForge 替换会重置本段 trim，`--edit` 因而要求明确选择导出范围。两例的 CPU 编辑合同与格式已验证，生成质量、原生编辑器导入和视听接缝仍需作者验收。

LongForge 已有工程的最小改稿流程，先取回当前版本与导出区间：

```sh
python examples/h3_longforge.py --project my-film --inspect
```

从输出的 `before-project.json` 复制 `head.revision`，从 `scene-2.json` 复制 `export_trim.revision`（可能为 `null`），写入 `edit.json`：

```json
{"scene": 2, "prompt": "VIDEO: Reach more slowly, pause, then touch the cup. SOUND: Continue room tone. MUSIC: N/A",
 "revision": "复制 head.revision", "trim_revision": null, "trim": "restore"}
```

```sh
python examples/h3_longforge.py --project my-film --edit edit.json
```

`restore` 明确恢复原区间；也可选 `full` 或 `{"in_frame":3,"out_frame":80}`。脚本保存改稿计划，替换选中段、提交批准的 trim，再导出下载；`film.mp4` 仍需审阅表演及音视频接缝。完整产物、失败后处理和适用工程范围见[改稿交付](../docs/editable-workflows.md)。

### 尚在完善的工作流

这两份脚本可用于阅读原生节点的组合方式，完整模型流程仍待跑通。

| 工作流 | 命令与前提 | 目标产物与当前限制 |
| --- | --- | --- |
| [Score to Screen](score_to_screen.py) · [官方来源](https://civitai.com/models/2975051) | `python examples/score_to_screen.py --image adult.png --audio song.wav --abc song.abc`；Python 3.12、四个原始 Score 节点包、llama-server 与 FastH3 Omni 环境 | 两段场景的 `film.mp4` 与文本；尚未完成成片。使用 graph 保留原节点的场景模板、隐藏图链接与动态展开 |
| [PerfectLooper](h3_perfect_looper.py) · [官方来源](https://civitai.com/models/3023857) | `python examples/h3_perfect_looper.py`；H3 环境、CoolB Nodes、H3 Motion Context 与 KJNodes | 闭环 MP4 与双次播放的接缝检查视频；尚未完成真实模型流程与闭环观感检查 |

## 首次准备节点包与权重

准备参数是显式安装动作。普通运行复用已装软件并检查所需节点；安装或更新前先等正在进行的下载和计算结束。模型、插件和输出遵守各自条款，节点存在也需要模型和硬件兼容。

| 脚本 | 节点包与准备参数 |
| --- | --- |
| 四个 Qwen 示例 | [ComfyUI-AusBoss](https://github.com/ausboss/ComfyUI-AusBoss)；`--install-plugin --download-models`。自动服装描述另需原生 `TextGenerate` |
| MOSS / Seed Hunter | 对应的当前原生后端；`--download-models` |
| Ideogram4 | [KJNodes](https://github.com/kijai/ComfyUI-KJNodes)、[Detail Daemon](https://github.com/Jonseed/ComfyUI-Detail-Daemon) 与原生 Ideogram4；`--install-plugins --download-models` |
| LongForge | 官方 LongForge NodePack；`--install-plugin --download-models`，同时准备 `filelock` / `imageio-ffmpeg` |
| Score to Screen | 官方 ABCScoreIO、PromptSplitter、ABCTiming、ABCInstrumentalize；`--install-plugins --prepare-llama --download-models` |
| PerfectLooper | [CoolB Nodes](https://github.com/CoolBreeze164/ComfyUI-CoolB-Nodes)、[H3 Motion Context](https://github.com/seitanism/ComfyUI-H3-Motion-Context-MultiRef)、KJNodes；`--install-plugins --download-models` |

LongForge/Score 的公开节点包来自官方附件。脚本中的 `NODE_ARCHIVE` 指定官方 URL、SHA256 与选定包目录，再交给 daemon 的 `plugins.install_archive()` 获取和安装；不需要在个人目录保存附件再上传。目标 daemon 必须能访问官方来源，这条附件下载路径尚待远端验证。

Score、Seed Hunter、LongForge 将模型来源解析后直接交给 `models.download(..., category=..., path=..., reuse=True)`。daemon 按原生模型目录顺序选择已有文件或下载位置，并核验所选文件的 SHA；脚本不需要重写模型根目录的查询与选择。

Score 默认使用 CPU LLM；`--llm-threads N` 按服务器实际 CPU 配额限制原工具线程。`--llm-device cuda` 选择原节点的 CUDA 构建与全部 GPU layers，目标实例需要兼容驱动和运行库。工具首次准备须显式加 `--prepare-llama`；普通运行不自动开始工具下载。

原 PromptSplitter 的 Linux CUDA 工具存在找不到已缓存 CUDA 运行库的已知问题，工具检查会因此失败；遇到此问题需要先修复原工具安装。

## 从这些工作流复用什么

- **固定准备，改变参数**：Qwen、MOSS、Ideogram4 在循环外保留输入与模型相关输出，循环中只改变 seed 或采样参数。
- **候选、选择、精修**：Seed Hunter 把视频和音频 latent 成对留在远端，先选候选，再执行昂贵阶段；Outfit 只取回小段描述供本地控制。
- **拆分后聚合**：Tiled 按块处理再拼接，CutOut 将物体提取与背景修补组合；中间 IMAGE/MASK 保持远端引用。
- **跨连接延续**：LongForge 用原生项目和不可变 take 保存进度，新 Client 继续使用同一项目。

需要研究精确 Tensor 取回、引用释放与计算/写盘重叠时，使用独立的 [H3 性能基准](../benchmarks/minimax_h3_reuse.py)，运行方式和历史测量见[性能记录](../docs/performance.md#现有做法与合理的工程投入)。它需要客户端 Torch/NumPy/Pillow，记录测量信息，不属于普通生图步骤。

工作流复现的历史运行与调优记录保存在[研究记录](../docs/research/workflow-reproduction-20261116.md)。
