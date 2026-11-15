# Comani

Comani 是面向 ComfyUI 的 Python SDK 与 daemon。把 **ComfyUI 看作执行设备，daemon 看作下发驱动**：本地调用节点、运行 Python 或提交原生工作流，结果以远端对象引用继续传递，需要时才取回本地。

当前版本为 `0.3.0`，源码位于 [`src/comani/`](src/comani/)。当前 daemon 入口使用部署者已配置的 ComfyUI 环境，直接启动 driver；Python、PyTorch、模型和机器部署由部署方准备。

## 能做什么

- **Eager 为主**：`remote.node("原始节点名", ...)` 调用立即进入发送流程，返回 `Call`；无需生成节点包装代码。
- **远端对象传递**：`OutputFuture` 可以直接作为后续输入；`await` 同步，`fetch()` 显式取回数据。
- **原生工作流**：`with remote.graph()` 捕获同一套调用，`remote.submit()` 一次提交；支持输入端口、组合及中间结果。
- **Python 代码执行**：支持匹配环境的 callable、自包含源码文件和目录包；目录包使用正常 Python imports。

一个 daemon 承载一个原生 ComfyUI runtime。当前原生执行使用单 worker；daemon 接收和依赖等待独立进行。工作流内部能否重叠计算，取决于 ComfyUI 和节点本身。

## 快速开始

先进入已能运行 ComfyUI 的 Python 3.11+ 环境，在同一解释器中安装轻量包并启动 daemon：

```sh
python -m pip install "git+https://github.com/Delva0/comani.git"
comani daemon --comfy-root /path/to/ComfyUI
```

`python -m comani daemon ...` 是同一入口。命令在前台运行，默认监听 `127.0.0.1:8190`；`--comfy-root` 选择已有 backend 源码。按硬件选择的原生参数放在 `--` 后，具体支持范围见 [启动与原生参数](docs/design/development.md)。

尚未发布 PyPI 版本，使用上面的 Git URL 或 `pip install /path/to/comani.whl`。安装不会 clone ComfyUI、创建 runtime venv 或下载模型。独立客户端只需同一轻量包，无需本地 Torch/ComfyUI；取回 Tensor 等对象时仍需对应库。

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

实际例程见 [本地 checkpoint](examples/local_checkpoint.py) 和 [H3 source-file 远端脚本](examples/minimax_h3_source_remote.py) / [source ops](examples/minimax_h3_source_ops.py)。公开 cd6 代码已在官方 ComfyUI 模板的 RTX 4090、Python 3.12.15 环境完成指定 INT8 模型三路 2048×2048 出图，完整 Tensor 逐位一致；环境、资源、清理及历史结果见 [独立远端报告](docs/remote_acceptance.md)。

![指定 Minimax H3 INT8 模型生成的 2048×2048 实际样图](artifacts/remote/60741609-20261115/eager.png)

## 适用边界

本项目提供 ComfyUI **后端**执行能力：原生 API prompt graph、节点与对象。前端 canvas JSON、WebUI 行为及所有第三方自定义 HTTP route 不在统一兼容承诺内；driver 仅实现少量辅助路由。

Eager 输出由有效会话持有，最后一个本地别名消失后通知释放；workflow 输出借用原生缓存，失效会报 `Expired`，不会自动重算。本地持有引用与原生 cache、模型 offload 是不同层，详见 [所有权与会话保活](docs/design/ownership.md)。目录包同名不同内容会报 `PackageConflict`，不会覆盖已安装模块。

公开 Call、未 release 的输出句柄与 Graph 共享保留所属 Client；删除局部 Client 变量不会单独关闭会话。显式 close/context 退出仍优先关闭整个会话。

HTTP 会话通过独立后台 keeper 续约；业务 loop 暂停后可继续使用仍有效的 owned refs，提交、读取和 GC 控制消息仍需要业务 loop 可运行。已验的 CPU 范围见 [启动与保活验收](tests/acceptance/HEARTBEAT_REPORT.md) 和 [压力报告](tests/stress/CURRENT_HEAD_PRESSURE_REPORT.md)，官方模板 GPU 结果及资源边界见 [远端报告](docs/remote_acceptance.md)。原生 CLI 的 parser 接受某个参数，不等于 headless driver 已实现对应服务；支持项和明确拒绝项见 [Runtime 边界](docs/design/runtime.md)。

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
| 已发布薄 CLI、metadata 与 CPU 基线 | [独立 0.3.0 报告](tests/acceptance/THIN_CLI_REPORT.md) |

历史环境与交付测量保存在 [`docs/`](docs/)，按报告中的源码版本和硬件范围解读；当前 API 以 `docs/design/` 和 `src/comani/` 为准。

## 开发

扩展节点、测试及 wheel 发布工具见 [开发指南](docs/design/development.md)，它们不是使用 daemon 的部署前提。
