# Comani

Comani 是管理并使用远端 ComfyUI 实例的 Python 库。一个普通 Python 脚本可以明确准备环境、固定版本、安装插件、获取模型、传输文件和运行 bash，再调用原生节点或 Python 代码出图。Tensor、模型和中间对象留在远端，按引用继续传递，需要时才取回本地。

一个带外 daemon 管理一个 ComfyUI 计算子进程。ComfyUI 未安装、已停止或启动失败时，管理接口仍可用；计算和原生对象留在 ComfyUI 进程里。客户端与管理环境只需轻量 Comani 包，管理 Python 和计算 Python 可以分开。

## 开始使用

当前版本为 `0.4.0`，尚未发布 PyPI 包。把本仓库构建的 wheel 安装到管理环境和客户端；也可从 Git 安装，部署时固定所需 revision：

```sh
python -m pip install /path/to/comani-0.4.0-py3-none-any.whl
comani daemon --comfy-root /srv/ComfyUI --workspace /srv --no-start
```

这会启动默认监听 `127.0.0.1:8190` 的管理服务，`/srv/ComfyUI` 可以尚不存在。为兼容已有启动命令，不加 `--no-start` 的 CLI 会尝试启动计算；**连接 Client 本身始终只做管理握手**，不会安装或启动 ComfyUI。远端入口使用 `COMANI_TOKEN` 和部署方提供的 SSH 转发或鉴权 HTTPS。

从空白实例准备 CPU 环境并生成 PNG，可直接运行 [basic.py](examples/basic.py)：

```sh
python examples/basic.py \
  --new-environment /srv/comfy-env \
  --comfy-root /srv/ComfyUI \
  --runtime-package /srv/packages/comani-0.4.0-py3-none-any.whl \
  --output artifacts/basic.png
```

前三个路径在 **daemon 所在机器**；`--output` 是客户端本地路径。`--runtime-package` 必须指向服务器已有的当前 wheel，或明确固定的 requirement。新环境目录必须不存在。脚本显式创建隔离 venv、配置 CPU、安装固定版本工具与 ComfyUI、准备计算依赖，然后启动计算；再次使用已有环境时省略三个准备参数。GPU 的 Torch/CUDA 版本由调用方明确选择，见 [环境准备](docs/design/remote-management-plan.md) 与 [H3 入口](docs/design/development.md#h3-完整脚本)。

## 普通 Python 调用

已配置的实例可以直接表达依赖与结果文件：

```python
import asyncio
import os
from pathlib import PurePosixPath

from comani import Client


async def main():
    async with Client(
        os.environ.get("COMANI_URL", "http://127.0.0.1:8190"),
        token=os.environ.get("COMANI_TOKEN"),
    ) as remote:
        info = await remote.comfyui.info()
        if info.state != "running":
            await remote.comfyui.start()

        image = remote.node("EmptyImage", width=64, height=64, batch_size=1, color=0x123456)
        inverted = remote.node("ImageInvert", image=image)
        saved = remote.node("SaveImage", images=inverted, filename_prefix="comani/demo",
                            return_call=True)
        await saved
        metadata = await saved.metadata()
        item = metadata.ui["result"]["images"][0]
        path = str(PurePosixPath(item["subfolder"]) / item["filename"])
        file = await remote.files.stat(path, directory=item["type"])
        await remote.files.download(file, "result.png")


asyncio.run(main())
```

`node()` 默认直接返回输出：单输出是一个可等待的输出，多输出是可解包、可等待的 tuple，零输出是 `None`。输出可立即传给下一个节点；需要就绪引用时再 `await`，需要本地对象时再 `await ref.fetch()`。调用立即进入发送流程，`await` 只等待，不触发提交。ready ref 本身不可 await。

需要调用 ID、metadata 或控制句柄时加 `return_call=True`，取得 `Call`；`.python()` 和 `.submit()` 仍返回 `Call`。默认 `declared_outputs=True` 按节点声明提供输出槽，Python 在解包时检查接收数：普通解包可能在任务已入队后立即报错，`await` 写法完成后才检查，不会撤回调用。设为 `False` 则按本地解包数提前选择输出槽。完整形状、声明与实际结果的边界见 [SDK](docs/design/sdk.md)。SaveImage 明确生成 8-bit PNG；fetch 保留精确浮点 Tensor。

`with remote.graph()` 只捕获 node/Python 计算调用，交给 ComfyUI 原生执行器；管理和 files 的 async 方法仍立即执行真实远端 I/O。无需节点包装代码、DSL 或另一个图编译器。

## 远端管理

| 接口 | 用途 |
| --- | --- |
| `remote.comfyui` | 查询实际配置/环境/源码版本，创建目标环境，显式安装、更新、准备和启停 |
| `remote.plugins` | 查询后端/插件/版本，安装指定 registry 包或 Git commit，启用、禁用与卸载 |
| `remote.models` | 在远端解析 HF/Civitai/直链、选择具体文件，校验下载或发布 HF snapshot |
| `remote.files` | 上传/下载、根目录、stat、校验和显式删除 |
| `remote.bash` | 远端非交互 bash；明确 cwd/env/timeout，默认非零退出抛错 |

这些查询都请求远端，不使用客户端目录或缓存冒充实例状态。返回的配置、版本、文件等 metadata 是普通本地值。

管理任务独立于 Client 和计算会话：已接纳的安装、下载或 bash 不因客户端断连或关闭而取消。普通 await 自动恢复原操作的观察；高级 `begin_*`、日志、取消和新 Client 附着见 [管理设计](docs/design/remote-management-plan.md)。失败与部分失败会抛错，不继续执行后续安装/启动步骤，也不声称回滚。

`comfyui.stop()` / `restart()` 默认 `wait=True`，等待计算和受管安装完成；显式 `wait=False` 紧急中断相关工作后停止或尝试重启，不保证安装回滚或重启成功。Python await 仍等待控制结果。bash 可与 runtime 并行，长命令不阻挡这些控制动作。

计算连接默认支持一小时的可配置断网宽限，恢复原 session、调用和对象；SDK 不重跑整段脚本。真正重启计算会使旧 refs 失效，borrowed refs 仍受原生缓存驱逐约束。文件传输自动续传同一个活动传输，完整校验后原子发布；已完成文件不随 GC 或 Client.close 删除。

## 示例与文档

| 内容 | 入口 |
| --- | --- |
| CPU 环境准备、bash、本地文件、eager/graph 与 PNG | [basic.py](examples/basic.py) |
| 远端 checkpoint 或显式上传本地 checkpoint | [local_checkpoint.py](examples/local_checkpoint.py) |
| H3 环境、插件、三模型准备与三模式生成 | [minimax_h3_remote.py](examples/minimax_h3_remote.py) |
| 同一 H3 流程的跨版本 source-file 计算 | [minimax_h3_source_remote.py](examples/minimax_h3_source_remote.py) |
| 有界复用与精确 Tensor 性能对照，保留 --serial | [minimax_h3_reuse.py](examples/minimax_h3_reuse.py) |
| 架构、SDK、管理、模型、文件与恢复 | [设计索引](docs/design/README.md) |
| 运行、开发、固定版本和 H3 参数 | [开发指南](docs/design/development.md) |
| 历史环境、性能与验收身份 | [文档目录](docs/README.md) |

Comani 提供 ComfyUI 后端的节点、对象和原生 prompt graph。WebUI、前端 canvas JSON、所有第三方 HTTP route、机器租用与多实例调度不在统一兼容范围。代码、插件和模型依赖仍须适合所选硬件与 backend revision。

历史 GPU/H3 图片与性能报告对应各自记录的源码、wheel 和硬件，不是本轮管理架构及改写示例的 GPU 验收。当前代码的独立验收与最终 wheel 身份应按新报告核对。

![历史 H3 INT8 2048×2048 实际样图](artifacts/remote/60741609-20261115/eager.png)

开发与打包见 [开发指南](docs/design/development.md)。可选 `comani[lz4]` 支持双方协商无损压缩，具体成本见 [协议](docs/design/protocol.md#二进制结果取回)。
