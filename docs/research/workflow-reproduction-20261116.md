# 工作流复现研究记录 · 2026-11-16

本页保存从 `examples/README.md` 移出的准备核对、CPU 调优假设和验收快照。它们属于当时的软件、模型、输入和执行记录，不是使用示例的步骤，也不替代后续运行结果。普通用户入口见[示例导览](../../examples/README.md)。

原始失败、运行产物和检查明细保留在本地 `.local/workflow-reproduction-20261116/` 与 `.local/workflow-reproduction-acceptance/`；本次文档整理不修改这些文件。历史严格验收使用特定质量标准，计数不代表框架能力或项目目标。

## Score 后续组件检查

整理时另行核对了 `.local/workflow-reproduction-acceptance/gpu-runs/score-cuda-component-v1/llm-prefix-result.json` 和 `library-resolution.json`：原 `build_scenes` 生成的第一个 LLM 的 11 节点祖先子图在 CUDA 路径完成，组件计时 5.942 秒，返回有效 JSON，`llm_saw_image=true`、`vision=sent`；CUDA 12/13 工具均实际枚举出 CUDA0。运行库检查使用已经缓存的官方库，没有下载或更改系统包、Torch 与原节点源码。

该检查保持先前的图片、音频、ABC、场景模板、线程默认值和 600 秒请求上限。CUDA 的 llama.cpp 构建为 b12438，先前 CPU 使用 b12436，因此它是组件兼容性与超时参照，不是单因素 GPU 加速比，也不代表完整 Score 已得到成片。下方旧快照中“尚无 GPU 成功”的文字只保留其历史含义。

## 官方附件准备快照

以下身份来自此前保存的官方 metadata；文档整理未重新请求附件。脚本中的 `NODE_ARCHIVE` 是当前 URL、SHA256 和选定包目录的配置入口。

LongForge/Score 脚本中的 `NODE_ARCHIVE` 记录已保存官方 metadata 的来源，未执行受限资源请求：

| 来源 | 固定附件身份 | 字节数 / SHA256 | 节点子目录 |
| --- | --- | --- | --- |
| AIBOB1199 · [LongForge v7.8](https://civitai.com/models/2926029) | version `3467080` / file `3370455` | `649345` / `5754f4594ed185577da67e1b4015139793bbfdbbeca0ef33b0a378b8185ccc0d` | `ComfyUI-H3-LongForge-NodePack/` |
| koyan0418439 · [Score to Screen v9.0](https://civitai.com/models/2975051) | version `3470191` / file `3377291` | `2056796` / `c6f55583038e259c441cebe8a0e31e9cfc727f9f4990e1dc65ed83488dad4b49` | `Score2Screen_v9.0/custom_nodes/` 下表列四包 |

Score 四包为 `ComfyUI-ABCScoreIO`、`ComfyUI-PromptSplitter`、`ComfyUI-ABCTiming`、`ComfyUI-ABCInstrumentalize`。官方说明未给出这些原包的独立 GitHub/registry 发布。`plugins.install` 接 registry ID，`install_git` 接 Git 仓库；例子用 `plugins.install_archive(NODE_ARCHIVE)` 表达官方 ZIP 来源，由 daemon 获取、校验 SHA、安装选定包目录并正常处理包内依赖。同一个 ZIP 的多个包共用一次获取，例子只提交缺失包；archive 不经过客户端。LongForge 未在 pyproject 声明的 filelock/imageio-ffmpeg 由例子显式安装，不从历史验收环境推断依赖已齐。`custom_nodes` 继续属于软件路径，不开放为数据上传根。官方 archive 的实际 daemon 下载与安装尚待验证，不以本地已有附件上传代替通过。

LongForge、Score、PerfectLooper 分别并发准备自己需要的独立模型文件；所有已开始的准备均结束并且全部成功后才加载模型。Score 先检查 Python 3.12 和原 `llama-server --version`，缺少工具时仅 `--prepare-llama` 允许原作者代码选择当前平台兼容的官方 release；不硬编码 b12436 或软件 commit。普通运行不悄悄进入首次工具下载。LongForge/Score 的 `--download-models` 对应脚本所列默认权重组，自定义 `--model` 等参数应先安装选定权重并省略此准备参数。

## 整理前的实验与验收快照

以下保留整理前的文字和当时结论，后续检查不能追记成这次运行已经完成。Score 的完整视频和 PerfectLooper 的真实模型流程在此快照中均未完成。

### Score 的 CPU LLM 线程

`--llm-threads N` 把 `-t N -tb N` 写入原生 `S2SLLMSettings.extra_args`，分别限制 llama.cpp 的推理和 batch 线程。省略参数保持原节点默认；不改变 ComfyUI、Torch 或 H3 的全局线程配置。

按服务器的进程 affinity 和 cgroup 配额选择 N，不能按宿主机 CPU 总数直接设置。一次实际环境的配额为 11.52 核，原 CPU LLM 默认开启 48 个推理线程；该环境下一次可显式试 `--llm-threads 11`，并比较同模型同输入的 LLM 阶段耗时。这是待验证的调整，不是通用默认，也尚无加速收益结论。没有 Comani 内置 CPU 配额查询 API；配额应由部署者的现有系统信息确认。

```sh
python examples/score_to_screen.py --image adult.png --audio song.wav \
  --abc song.abc --llm-threads 11
```

该命令只适用于已完成 Score 节点包、llama 工具和模型准备的实例。不要在当前 LLM/采样期间更新脚本部署、改节点包或启动另一轮任务。

`--llm-device cuda` 保留同一 GGUF/F16 视觉权重，只选择原节点的 `llama_build=cuda` 与 `gpu_layers=all`。工具预检必须通过 `--list-devices` 实际枚举 CUDA 设备，CPU 默认不变。原节点在 LLM 前释放 ComfyUI 模型显存，并在 `unload_after=True` 时关闭本地 server，随后才进入 H3。官方 llama.cpp 提供 [Ubuntu CUDA 构建](https://github.com/ggml-org/llama.cpp/releases)，但所选 release 的驱动、运行库和 GPU 兼容性仍需在目标实例确认；新增参数目前没有 GPU 成功或性能收益证据。下一步应重放原失败的 PromptSplitter 输入做同参 GPU 对照，不通过延长 600 秒请求掩盖 CPU 超时。

### 复现与验收边界

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
