# 运行、示例与开发

客户端和管理 daemon 需要 Python 3.11+ 与轻量 Comani 包。ComfyUI、Torch、节点和模型属于明确选定的计算环境。以下命令用于 Linux/WSL；服务器路径和客户端路径分别标明。

## 启动管理 daemon

在服务器管理环境安装当前 wheel，然后选择实际 root、计算解释器和 workspace：

```sh
python -m pip install /server/packages/comani-0.5.0-py3-none-any.whl
comani daemon --comfy-root /server/ComfyUI \
  --runtime-python /server/comfy-env/bin/python --workspace /server --no-start
```

ComfyUI 和计算环境路径可以尚不存在；此时先执行下面的显式准备。`python -m comani daemon` 与 console 入口相同。默认前台监听 127.0.0.1:8190，由 Ctrl-C/SIGTERM 或部署者进程管理器停止。`--no-start` 只启动管理，不加时 CLI 尝试启动计算；Client 连接始终只做管理握手。

通过 `--config /server/runtime.json` 保存/加载配置，明确 flags 覆盖配置。API configure 的持久化需要 daemon 配置了该文件，否则仅当前进程有效。`--print-config` 查看有效配置。远端入口使用部署方 SSH 转发或鉴权 HTTPS；token 来自 COMANI_TOKEN 或 --token，非 loopback 绑定必须有 token。

## 模型目录与代码更新分开

同一实例保留磁盘时，重启计算、重启 daemon、替换 Comani wheel 都不要求重新下载模型。ComfyUI 的原地 Git 更新也不删除已有模型；把 ComfyUI 改装到另一个目录时，默认 `models/` 会随代码目录改变，旧文件可能仍在磁盘却不在新搜索路径里。

部署时把模型和配置放在代码目录之外，使用 ComfyUI 原生的 `extra_model_paths`。例如先创建 `/server/comani-data/models/` 下需要的类别目录，再保存 `/server/comani-data/extra_model_paths.yaml`：

```yaml
persistent_models:
  base_path: /server/comani-data
  is_default: true
  checkpoints: models/checkpoints
  diffusion_models: models/diffusion_models
  text_encoders: models/text_encoders
  vae: models/vae
  loras: models/loras
```

`is_default: true` 把这些目录放在原生优先级前面。只声明实际需要的类别；插件自注册的类别仍遵循插件自己的准备流程。把下面保存为 `/server/comani-data/runtime.json`，然后运行 `comani daemon --config /server/comani-data/runtime.json --no-start`：

```json
{
  "comfy_root": "/server/ComfyUI",
  "runtime_python": "/server/comfy-env/bin/python",
  "workspace": "/server/comani-data",
  "comfy_args": [
    "--extra-model-paths-config",
    "/server/comani-data/extra_model_paths.yaml"
  ]
}
```

之后停止计算并更新 core/wheel，或将 `comfy_root` 显式切换到已准备好的新代码目录，保留这份配置和模型目录。`configure(comfy_args=[...])` 会替换整个参数列表，增改设备参数时要保留上述配置参数。已有模型不会自动搬家；迁移旧部署时先停止计算，再由部署者移动或挂载原模型目录并核对文件，避免把含模型的旧目录一并删除。

普通准备调用继续使用同一模型文件名与内容 SHA，并传 `reuse=True`。相同可见路径和 SHA 的文件会在源 GET 前复用；本地 SHA 检查仍有磁盘读取成本。新代码目录中出现同名但不同内容的高优先级文件会报冲突，不会用另一个根中的文件掩盖它。

磁盘是否跨实例保留由部署平台决定。停止同一实例与销毁后另租一台是不同动作；独立目录配置不能让已销毁的磁盘恢复，也不能把本地盘变成跨主机共享盘。本地回归可运行 `python -m pytest tests/integration/test_model_persistence_story.py -q`：真实 CLI daemon、CPU ComfyUI 和计数 HTTP 源验证首次下载一次，计算重启、daemon 换进程和代码换目录后均零 GET，并读取同一小型 safetensors 的内容。

## 空白 CPU 环境

先启动纯管理 daemon。在客户端把下面保存为 `prepare_cpu.py` 并运行，填写真实服务器路径；wheel 必须已经存在于服务器。新 venv 目录必须不存在。

```python
import asyncio
import os

import comani


async def main():
    async with comani.Client(
        os.environ.get("COMANI_URL", "http://127.0.0.1:8190"),
        token=os.environ.get("COMANI_TOKEN"),
    ):
        env = await comani.comfyui.create_environment("/server/comfy-env")
        await comani.comfyui.configure(
            runtime_python=env.python, comfy_root="/server/ComfyUI",
            cpu=True, custom_nodes=False,
        )
        await comani.comfyui.prepare_tools(skip_manager=True)
        await comani.comfyui.install(skip_torch=False, skip_manager=True)
        await comani.comfyui.prepare_runtime("/server/packages/comani-0.5.0-py3-none-any.whl")


asyncio.run(main())
```

这些是明确的安装动作。缺失 comfy-cli 时安装发行版，首次 ComfyUI 安装默认选择上游 nightly/default branch，并记录实际 commit；不固定一次历史验收的 SHA。CPU 的 Torch 由 `install(skip_torch=False)` 按 CPU 配置安装。已有环境可以直接 configure 其解释器，无需重新创建。准备结束后使用 `basic.py` 启动计算并处理图片：

```sh
python examples/basic.py --input photo.png --output artifacts/basic.png
```

只需要本地图片和轻量客户端，不需要 checkpoint 或本地 Torch。脚本上传输入、执行 LoadImage → inline Python → ImageInvert → SaveImage，再用 `Call.file()` 和 `files.download` 下载。输入在读取完成后删除，远端结果保留；本地输出 create-only，重复运行请选择新路径。它不要求 CPU 专用配置，也可复用 GPU 实例。

## graph 导出接 eager 下游

```sh
python examples/graph_to_eager.py --output artifacts/graph-to-eager.png
```

[graph_to_eager.py](../../examples/graph_to_eager.py) 不需要模型或本地 Torch。它把 eager EmptyImage 绑定到 graph 输入，图内 ImageInvert 声明一个导出；`processed = comani.submit(workflow, inputs=...)` 默认直接给 Output，再交给图外 ImageInvert，最后保存下载。两次反转近似恢复原始 RGB(18,52,86)，浮点计算后保存为 8-bit PNG 可能每通道相差一级。

此处没有 Call 包装或 .output() 胶水。需要 UI/文件句柄的 checkpoint/H3 图则显式 `submit(..., return_call=True)`。零导出的默认 submit 返回 None 仍已提交；需要等待整图时也选 True。计算、逐节点观察与返回形状见 [SDK](sdk.md#graph-导出直接接-eager-下游)。

## checkpoint 工作流

```sh
python examples/local_checkpoint.py --checkpoint my-model.safetensors \
  --prompt "a red panda in a sunlit garden" --output artifacts/panda.png
```

`--checkpoint` 是 ComfyUI 原生模型名。要上传本地 checkpoint，增加 `--upload-checkpoint /local/model.safetensors`；多个 checkpoint 根时还需 `--model-root /actual/server/checkpoints`，只有一个根可直接使用它。上传是 create-only，模型保留供以后使用。选择的权重须适合可见的 CheckpointLoaderSimple/CLIP/KSampler/VAE 节点链和目标硬件；示例不替你选择模型体系。

这个脚本使用显式 `async with Client(...) as remote`，与 basic/H3 的模块上下文风格相互对应。graph 仅捕获计算，SaveImage 默认是原生执行根，通过 submit(graph, return_call=True) 保留 Call，下载用 run.file(node_id=saved.id) 获取实际结果。

## H3 完整脚本

[minimax_h3_remote.py](../../examples/minimax_h3_remote.py) 用同一脚本表达环境/插件准备、三个权重的获取屏障和一次原生 graph 推理。普通运行复用已安装 core、工具和 GPU 配置：

```sh
python examples/minimax_h3_remote.py --output artifacts/h3.png
```

`--prompt/--seed/--width/--height/--steps` 直接控制脚本中可见的 H3 节点链；采样器、scheduler、CFG、loader 类型和三个模型名也明确写在脚本中，可按需要修改或插入节点。没有模型专属的隐藏 pipeline。

需要准备或修改目标时先显式停止计算，然后选择所需路径和依赖：

```sh
python examples/minimax_h3_remote.py --prepare \
  --runtime-python "$RUNTIME_PYTHON" --comfy-root "$COMFY_ROOT" \
  --runtime-package "$SERVER_COMANI_WHEEL" --output artifacts/h3.png
```

`--prepare` 复用已装 core/工具，缺 core 才 install；不会默认更新已装 core。增加 `--update` 才沿当前 upstream 更新；`--comfy-version nightly|latest|tag` 或 `--comfy-commit 完整SHA` 明确选择目标，并在已安装时更新到该目标。detached HEAD 没有当前 upstream，更新时必须指定目标。

新 GPU venv 使用 `--new-environment /server/new-env` 代替 `--runtime-python`，并必须传 `--runtime-package`、重复的 `--torch-requirement` 和 `--torch-index-url`。GPU 的 Torch/CUDA build 由调用者根据机器选择，不由脚本猜测。已有目标仅在给了这些包参数时安装它们。除非明确给版本，prepare_tools 保留已装工具；缺 Manager 时按所选 core 的 manager_requirements 安装，缺该文件时使用发行版。

软件选择参数都在 `--prepare` 组：`--cli-version` 选择 comfy-cli PyPI 版本，`--cli-revision` 选择 Git ref，二者互斥；`--manager-version` 和 `--hub-version` 明确选择对应包。插件用重复的 `--plugin registry-id@version` 或 `--git-plugin repository-url@完整commit`；修改后显式 start 才加载 registry。

本地 `models.resolve` 查询 Civitai version3462708/file3365259 和 HF commit0bb149f5… 的 encoder/VAE，保留模型内容 SHA 校验；真正下载由 daemon 完成。`HF_TOKEN/CIVITAI_TOKEN` 的本地查询权限与 daemon 下载权限独立配置。每类只有一个模型根时直接选它；多个根时必须给 `--diffusion-root/--encoder-root/--vae-root`，值是服务器实际绝对路径。三路下载使用 asyncio.gather，全部成功才构图；它不是跨根的原子事务。

脚本启动后检查实际 registry 是否有 H3 所需节点，不对软件 commit 做默认等式检查。loader 类型、输入端口和实际模型兼容性仍由 ComfyUI 原生验证/执行检查；节点存在不代表模型或硬件一定可用。`--transport auto` 复用目标已装 hub/Xet/aria2 能力，`http|aria2` 可显式选择；不自动安装系统工具。

普通输出只有 native SaveImage 8-bit PNG，无计时 sidecar 或精确 Tensor 对照。[minimax_h3_source_remote.py](../../examples/minimax_h3_source_remote.py) 在同一 source 文件定义纯函数和 async 入口，用 `source=Path(__file__)` 向上述图插入计算；本地入口的 imports 放在 main guard 中，不要求服务器存在本地 example 模块。inline 字符串、源码文件、目录包与 callable 的选择见 [Python artifacts](python-artifacts.md)。

## 性能基准

[benchmarks/minimax_h3_reuse.py](../../benchmarks/minimax_h3_reuse.py) 保留 model/conditioning refs，最多一个生成和一个 fetch/save 在途，`--serial` 关闭重叠。它明确依赖客户端 Torch/NumPy/Pillow，并记录 setup、生成、下载、写盘和构建身份；这是性能研究需要的复杂度，不是普通生图步骤。

```sh
python benchmarks/minimax_h3_reuse.py --output artifacts/h3-benchmark \
  --seeds 426390 426391 426392
```

可选 `--expected-python/--expected-torch/--expected-comfy-commit` 为复现实验主动断言已装环境，不是默认安装目标。历史冻结脚本、模型 hash、测量和图片保留于各自 artifacts/report；当前脚本的 CLI、语法或 CPU 结果不能充当新的 H3 GPU 数值认证。

## 原生参数与管理行为

原生参数放在 daemon 的 `--` 后，例如 `-- --lowvram --reserve-vram 4`，或 configure(comfy_args=[...])。设备环境在 Torch 导入前应用。节点名直接取 registry，不生成本地包装类；前端 JS、所有第三方 HTTP route 和官方 WebUI 不在统一兼容范围。

环境、core、插件变更需显式停止计算；bash 是独立 OS 通道，可与 runtime 并行。`stop()/restart()` 默认 wait=True；显式 wait=False 紧急中断相关工作后停止/尝试重启，不保证安装回滚。就绪、恢复和 Operation 细节见 [管理设计](remote-management-plan.md)。

## 测试与打包

选择具备目标 ComfyUI/Torch 的开发解释器；SDK-only 用例不需要 native 依赖：

```sh
python -m pip install -e ".[test]"
python -B -m pytest -q -m 'not gpu' -p no:cacheprovider
python -m ruff check src tests scripts examples benchmarks
python -m pip wheel --no-deps --wheel-dir dist .
```

unit 验证 SDK/故障/打包，native 验证 cache/lazy/loop/expand/list，integration 验证 HTTP/隔离环境/管理与计算。GPU 测试通过 COMANI_TEST_URL 和显式 COMANI_TEST_CHECKPOINT 选择已有权重，不自动下载或租机。

最终 wheel 在源码/README 冻结后构建，并核对 source/archive/独立安装身份。历史 [wheel_manifest.json](../wheel_manifest.json) 是 0.2.0 快照；旧报告不得借给新构建。[verify_wheel.py](../../tests/acceptance/verify_wheel.py) 必须使用本次真实 SHA。scripts/ 保留必要 [环境诊断](../../scripts/check_environment.py) 与 [wheel 刷新](../../scripts/refresh_wheel.py)。

示例作者的 AST/ruff/help 检查、独立 CPU 执行、新 GPU/WAN 验收和最终发布身份是不同证据，应分别记录。
