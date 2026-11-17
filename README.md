# Comani

Comani 是管理并使用远端 ComfyUI 的 Python 库。一个脚本可以显式准备环境、安装插件、取得模型，再自由组合原生节点和 Python 代码。Tensor、模型和中间对象留在远端；需要本地对象时才 fetch，需要图片文件时保存并下载。

带外 daemon 管理一个 ComfyUI 计算子进程。ComfyUI 未安装、已停止或启动失败时，管理、bash 和文件接口仍可用。客户端和管理环境只需轻量 Comani 包；Torch、节点和模型属于计算环境。

## 开始使用

当前版本为 `0.5.0`，尚未发布 PyPI 包。在客户端和服务器管理环境安装本仓库构建的 wheel：

```sh
python -m pip install /path/to/comani-0.5.0-py3-none-any.whl
```

服务器上已有 ComfyUI 时，用其实际路径和解释器启动管理服务：

```sh
comani daemon --comfy-root /srv/ComfyUI \
  --runtime-python /srv/comfy-env/bin/python --workspace /srv --no-start
```

管理入口默认是 `127.0.0.1:8190`。远端连接使用部署者提供的 SSH 转发或鉴权 HTTPS，通过 `COMANI_URL`、`COMANI_TOKEN` 选择入口。**Client 连接只做管理握手，不安装或启动计算。** CLI 不加 `--no-start` 时会尝试启动计算。

HTTP 保持默认。需要本地 IPC 和远端 TCP 共用计算通道时，可显式安装 `comani[zmq]` 并选择 `--compute-transport zmq` / `--zmq-bind`；同一个 Client 通过 HTTP companion 使用管理和文件接口，部署与 CURVE 配置见 [ZMQ 计算通道](docs/design/compute-transport.md)。

如果服务器还没有 ComfyUI，先用相同 daemon 命令启动纯管理入口，路径可以尚不存在，然后按 [空白 CPU 环境准备](docs/design/development.md#空白-cpu-环境) 创建环境。GPU 的 Torch/CUDA 包由调用者选择；[H3 完整脚本](examples/minimax_h3_remote.py) 展示同一脚本中的显式准备、模型、插件和推理。

daemon 部署启动时会准备 `aria2c`；默认或显式选择 aria2 的模型下载在需要传输时也会自行检查，缺失时通过宿主系统包管理器自动安装并验证版本。安装需要 daemon 的 root 或非交互 sudo 权限；失败会记录阶段与原因，带外管理仍可用于修复，不会静默换引擎。完整 SHA 命中的 `reuse=True` 无须网络或下载器，保留离线复用。调用者仍可主动选择 `transport="http"` 或 `transport="hf"`；HF 使用目标解释器中的官方 Hub 包。详见[模型下载](docs/design/models.md)。

准备好实例后，给一张本地图片即可运行第一个示例：

```sh
python examples/basic.py --input photo.png --output artifacts/basic.png
```

[basic.py](examples/basic.py) 上传图片，用 LoadImage 读取，执行一小段 Python 和 ImageInvert，再用 SaveImage 生成 PNG 并下载。它不需要模型或客户端 Torch，也不改变实例的硬件配置；CPU/GPU 已配置环境都可使用。输入在计算完成后删除，生成的远端 PNG 保留；本地输出默认不覆盖已有文件。

## 像普通 Python 一样组合

模块函数使用最近进入的显式 Client 上下文，因此 helper 不必层层传入 `remote`：

```python
import asyncio
import os

import comani


async def main():
    async with comani.Client(
        os.environ.get("COMANI_URL", "http://127.0.0.1:8190"),
        token=os.environ.get("COMANI_TOKEN"),
    ):
        await comani.comfyui.start()
        image = comani.node("EmptyImage", width=64, height=64, batch_size=1, color=0x123456)
        adjusted = comani.python(
            code="def adjust(image):\n    return image * 0.5\n",
            entry="adjust", inputs={"image": image},
        )
        saved = comani.node("SaveImage", images=adjusted,
                            filename_prefix="comani/demo", return_call=True)
        file = await saved.file()
        await comani.files.download(file, "result.png")


asyncio.run(main())
```

原始节点名和输入端口直接来自 ComfyUI。`node()/python()/submit()` 默认直接返回输出，可立即传给后续节点；submit 按显式 graph 导出数返回 None、一个输出或可解包的 tuple。需要调用 ID、metadata、文件结果或控制句柄时加 `return_call=True`。`python()` 的 tuple/dict/None 返回值仍占一个输出槽。取本地 Python 对象用 `await ref.fetch()`；SaveImage 则明确生成 8-bit PNG。

显式 Client 风格同样可用，适合同时操作不同实例：

```python
async with comani.Client(url, token=token) as remote:
    await remote.comfyui.start()
    image = remote.node("EmptyImage", width=64, height=64, batch_size=1, color=0)
    inverted = remote.node("ImageInvert", image=image)
```

嵌套 Client 的模块调用使用最内层上下文，退出后恢复外层；显式 `remote.node(...)` 始终使用该 Client。子 task 可以继承上下文，应在 Client scope 内完成；退出后不能继续发起模块调用。它不创建隐式连接，也不改变引用的实例归属。

短小、自包含的远端逻辑用 `code=源码字符串, entry=函数名`；已有脚本用 `source=Path(__file__)`，多个相互导入的模块用 `source=包目录`。源码由目标 Python 解释，不需要与客户端相同版本，但必须适合目标语法和依赖。Callable 传输仍要求双方 Python/cloudpickle 的精确版本匹配，见 [Python 代码](docs/design/python-artifacts.md)。

## 图与结果

`with comani.graph()` 捕获 node/Python 调用，退出后再提交；一个导出就直接给一个可传递的输出：

```python
source = comani.node("EmptyImage", width=64, height=64, batch_size=1, color=0x123456)
with comani.graph() as workflow:
    image = workflow.input("image", type="IMAGE")
    inverted = comani.node("ImageInvert", image=image)
    workflow.output("image", inverted)

processed = comani.submit(workflow, inputs={"image": source})
restored = comani.node("ImageInvert", image=processed)
```

完整无模型故事见 [graph_to_eager.py](examples/graph_to_eager.py)：eager 图像进入 workflow，默认 submit 输出直接接 eager 下游，再保存下载。节点链和输入/导出都可见。上传、管理和 provider 查询是普通 async I/O，不会因位于 graph scope 内而被捕获。

默认 submit 输出等待整个 workflow 完成。需要 ID、UI/文件、控制或 `run[ref]` 逐节点观察时用 `submit(..., return_call=True)` 保留 Call。没有导出的 graph/raw prompt 默认返回 None，但仍已提交；要等待它或取得 SaveImage 文件也须显式保留 Call。

图有声明输入时才传 `submit(workflow, inputs={...})`。命名导出用 `workflow.output(name, ref)`，执行目标用 `workflow.target(call_or_ref)`，都在构图时声明。普通原生 API prompt 可以直接 `submit(prompt)`；需要命名导出或自选根时先 `graph(prompt, outputs=..., targets=...)`。SaveImage 等原生输出根默认仍执行，增加导出不会把它们删掉。三者区别见 [SDK/graph](docs/design/sdk.md#submit-输入导出与执行根)。

Call 只有在 metadata.ui 含标准 ComfyUI 文件描述符时才产出文件；单文件用 `await call.file()`（要求恰好一个），零个或多个用 `await call.files()` 得到 tuple；workflow 可按 `node_id=saved.id` 筛选。它们返回 RemoteFile，接着调用 `files.download` 即可，不必手解 UI descriptor、拼路径或 stat。原始 `metadata()` 仍保留。

## 本地查询与远端工作

| 接口 | 执行位置与职责 |
| --- | --- |
| `comani.models.resolve/versions/manifest` | 客户端查询 HF/Civitai，固定 artifact/revision；无需 Client，使用本地或显式 provider 凭证 |
| `comani.plugins.versions` | 客户端查询公开插件 registry；无需 Client |
| `comani.comfyui` / `remote.comfyui` | 远端实际配置、环境、显式准备、安装、更新与启停 |
| `comani.plugins` / `remote.plugins` 的其他方法 | 远端插件状态与安装、启禁用、卸载 |
| `comani.models` / `remote.models` 的其他方法 | 远端模型根与文件状态、下载、snapshot 和删除 |
| `comani.files/bash/operations` | 远端文件传输、非交互 bash 和受管任务观察 |

纯 provider 查询不放在 `remote.models` 或 `remote.plugins` 上。本地解析结果可带到所选 daemon 下载；下载使用 daemon 的网络和凭证，本地查得到不代表远端有权限下载。普通或 signed HTTP URL 直接交给远端 `models.download(url, root=..., path=...)`，无需把秘密 URL 注册成客户端 artifact。

默认复用已安装软件并检查所需能力；显式安装、更新和 revision 选择才改变软件。没有历史 ComfyUI/comfy-cli/Manager commit 门禁。模型内容 SHA 和选定的模型 revision 仍用于确认实际权重。

受管安装、模型下载和 bash 独立于计算会话：已接纳操作在断连后继续，普通 await 恢复观察原操作；失败中断后续步骤，不承诺回滚。文件传输在活动 await 内自动续传并校验；已完成文件不随 Client.close 删除。计算重启使旧 refs 失效，详见 [管理](docs/design/remote-management-plan.md)、[文件](docs/design/files.md) 与 [所有权](docs/design/ownership.md)。

## 示例与文档

| 用户故事 | 入口与前提 |
| --- | --- |
| 上传图片、节点、少量 Python、保存下载 | [basic.py](examples/basic.py)：已配置实例 + 一张本地图片 |
| graph 单导出直接接 eager 下游 | [graph_to_eager.py](examples/graph_to_eager.py)：已配置实例，无模型或客户端 Torch |
| 修改 checkpoint 生图工作流 | [local_checkpoint.py](examples/local_checkpoint.py)：兼容 checkpoint，已在远端或显式本地上传；展示显式 Client 与 CLIP/KSampler/VAE 节点 |
| 单脚本环境、插件、三模型与 H3 生图 | [minimax_h3_remote.py](examples/minimax_h3_remote.py)：H3 可用 GPU 环境；可显式准备 |
| 从当前脚本发送远端 Python 源码 | [minimax_h3_source_remote.py](examples/minimax_h3_source_remote.py)：相同 H3 前提，纯函数与 async 入口在同一文件 |
| 图像编辑、布局与视频工作流 | [按任务选择示例](examples/README.md)：运行命令、依赖、产物与使用前提 |
| 保留 refs、有限重叠与 Tensor 性能基准 | [benchmarks/minimax_h3_reuse.py](benchmarks/minimax_h3_reuse.py)：H3 环境 + 客户端 Torch/NumPy/Pillow |
| 当前 API、环境准备、模型与文件 | [设计索引](docs/design/README.md)、[运行指南](docs/design/development.md) |
| 历史环境、图片、性能与验收身份 | [文档目录](docs/README.md) |

Comani 提供原生后端节点、对象和 API prompt graph。WebUI、前端 canvas JSON、所有第三方 HTTP route、机器租用和多实例调度不在统一兼容范围。模型、插件与节点必须适合所选硬件和 backend。

项目附带 [Vast 官方 skill](.agents/skills/vastai/SKILL.md)，配合单独安装的官方 CLI 租用与管理机器；连接可用后按 [Comani 交接说明](.agents/skills/vastai/references/comani.md) 转到现有 daemon、SDK 和示例。

历史 GPU/H3 结果属于各报告记录的源码、wheel 和硬件；改写后的示例不自动继承旧脚本的数值认证。

![历史 H3 INT8 2048×2048 实际样图](artifacts/remote/60741609-20261115/eager.png)

开发与打包见 [开发指南](docs/design/development.md)。可选 `comani[lz4]` 提供协商后的无损压缩，见 [协议](docs/design/protocol.md#二进制结果取回)。
