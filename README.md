# Comani

Comani 是面向 ComfyUI 的 Python SDK 与 daemon。把 **ComfyUI 看作执行设备，daemon 看作下发驱动**：本地调用节点、运行 Python 或提交原生工作流，结果以远端对象引用继续传递，需要时才取回本地。

当前版本为 `0.2.0`，仓库根目录是项目入口，源码位于 [`src/comani/`](src/comani/)。当前先重建通用的后端执行基础；旧项目已单独归档，后续逐步迁入其功能。

## 能做什么

- **Eager 为主**：`remote.node("原始节点名", ...)` 调用立即进入发送流程，返回 `Call`；无需生成节点包装代码。
- **远端对象传递**：`OutputFuture` 可以直接作为后续输入；`await` 同步，`fetch()` 显式取回数据。
- **原生工作流**：`with remote.graph()` 捕获同一套调用，`remote.submit()` 一次提交；支持输入端口、组合及中间结果。
- **Python 代码执行**：支持匹配环境的 callable、自包含源码文件和目录包；目录包使用正常 Python imports。

一个 daemon 承载一个原生 ComfyUI runtime。当前原生执行使用单 worker；daemon 接收和依赖等待独立进行。工作流内部能否重叠计算，取决于 ComfyUI 和节点本身。

## 快速开始

需要 Python 3.11+、`uv` 和已有的 ComfyUI 源码目录。下面是本地 Linux/WSL 开发流程，替换 ComfyUI 路径与 Python 解释器即可：

```sh
cd /path/to/comani
python3 scripts/bootstrap_runtime.py \
  --python python3.11 --comfy-root /path/to/ComfyUI
.venv/bin/python -B scripts/dev_server.py start \
  --comfy-root /path/to/ComfyUI --port 8190
.venv/bin/python -B scripts/example.py --url http://127.0.0.1:8190
.venv/bin/python -B scripts/dev_server.py stop
```

Bootstrap 使用独立的 `.venv`，不替换原 ComfyUI 环境，也不复制模型。完整环境前提、启动方式和测试命令见 [开发与本地部署](docs/design/development.md)。

已有 daemon 时，在本目录只安装本地 SDK：

```sh
python -m pip install .
```

SDK 本身不依赖 Torch/ComfyUI；若要在本地反序列化 Tensor 等对象，本地仍需相应库。
运行示例时使用仓库根目录，或在其他工作目录使用已安装的 SDK。

## 一段 Python

以下 callable 示例要求客户端与 daemon 的 Python/cloudpickle 版本匹配；跨环境代码可显式选择 [源码文件或包](docs/design/python-artifacts.md)。

```python
import asyncio
from comani import Client

async def main():
    async with Client("http://127.0.0.1:8190") as remote:
        image = remote.node(
            "EmptyImage", width=64, height=64, batch_size=1, color=0x123456,
        )
        adjusted = remote.python(
            lambda image: image * 0.5, inputs={"image": image.output(0)},
        )
        inverted = remote.node("ImageInvert", image=adjusted.output(0))
        ref, = await inverted
        local_image = await ref.fetch()
        print(local_image.shape)

asyncio.run(main())
```

`await call` 返回按槽排列的 ready refs；`await call.output(i)` 返回其中一个。`RemoteRef` 本身不可 await。无关调用不会因一次本地 await 获得额外的远端顺序依赖。

完整脚本见 [本地最小示例](scripts/example.py)、[本地已有 checkpoint 出图](examples/local_checkpoint.py) 和 [Minimax H3 远端执行脚本](examples/minimax_h3_remote.py)。远端脚本从环境读取 URL/token，已在指定 INT8 模型上完成 eager、captured graph 与 raw API graph 三路 2048×2048 出图；三路原始 Tensor 与 PNG 均完全一致。运行环境、显存参数、资源与清理记录见 [独立远端报告](docs/remote_acceptance.md)。

![指定 Minimax H3 INT8 模型生成的 2048×2048 实际样图](artifacts/remote/60662518-20261114/eager.png)

## 适用边界

本项目提供 ComfyUI **后端**执行能力：原生 API prompt graph、节点与对象。前端 canvas JSON、WebUI 行为及所有第三方自定义 HTTP route 不在统一兼容承诺内；driver 仅实现少量辅助路由。

Eager 输出由会话持有，最后一个本地别名消失后通知释放；workflow 输出借用原生缓存，失效会报 `Expired`，不会自动重算。目录包同名不同内容会报 `PackageConflict`，不会覆盖已安装模块。

[本地独立验收](tests/stress/INDEPENDENT_LOAD_REPORT.md) 已通过：非 GPU suite 为 81 passed、2 deselected，覆盖持续提交、复杂图、GC/会话回收，以及本地 125 秒读取超时代理上的 140 秒调用。真实远端 HTTPS 的基础执行、130 秒长任务和指定 GPU 模型出图也已验证，租用实例已销毁，详见 [远端报告](docs/remote_acceptance.md)。这些结果不代表原生计算并行或性能上限。

目录提升与原生启动参数透传后的独立标准非 GPU 回归为 **89 passed、2 deselected**，包含真实原生 runtime、SDK-only 安装和无 Torch 的 wheel HTTP 客户端。

## 文档

| 内容 | 入口 |
|---|---|
| 总体架构与组件地图 | [设计索引](docs/design/README.md) |
| 调用、await、graph 与中间量 | [SDK 契约](docs/design/sdk.md) |
| daemon 与原生执行器边界 | [Runtime 设计](docs/design/runtime.md) |
| GC、所有权、缓存与会话 | [对象生命周期](docs/design/ownership.md) |
| RPC、引用编码、错误和事件 | [协议](docs/design/protocol.md) |
| callable、文件和目录包 | [Python artifacts](docs/design/python-artifacts.md) |
| 环境、运行、扩展节点与测试 | [开发指南](docs/design/development.md) |
| 环境与验收记录 | [文档目录](docs/README.md) |

历史环境与交付测量保存在 [`docs/`](docs/)，新增结果见 [独立压力报告](tests/stress/INDEPENDENT_LOAD_REPORT.md) 和 [独立远端报告](docs/remote_acceptance.md)。当前 API 以本仓库的 `docs/design/` 和 `src/comani/` 为准；旧项目资料保留在独立的 `comani-old` 归档目录。

## 开发

本机环境、扩展节点、测试、wheel 刷新及部署步骤见 [开发指南](docs/design/development.md)。环境版本快照见 [environment.json](docs/environment.json)，完整组件地图见 [设计索引](docs/design/README.md)。
