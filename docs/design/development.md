# 开发、本地部署与节点扩展

当前项目入口是仓库根目录的 [`pyproject.toml`](../../pyproject.toml)。下面命令在 Linux/WSL 的 `comani/` 仓库根目录执行。

## 环境

前提：Python 3.11+、`uv`、已有的 ComfyUI 源码 checkout，以及与硬件匹配的原生依赖。

```sh
cd /path/to/comani
python3 scripts/bootstrap_runtime.py \
  --python /path/to/python3.11 --comfy-root /path/to/ComfyUI
.venv/bin/python -B scripts/check_environment.py \
  --comfy-root /path/to/ComfyUI --cuda
```

[Bootstrap](../../scripts/bootstrap_runtime.py) 在 `.venv` 安装 ComfyUI requirements 与本项目，并使用 [runtime constraints](../../runtime-constraints.txt)。它不修改原 ComfyUI venv、源码或模型目录；这不是自动下载模型的步骤。

可选 `--seed-from /path/to/ComfyUI/.venv` 仅用于新环境，要求源和目标 Python 版本完全一致且源无 editable 导入；它复制第三方 site-packages 后重新解析依赖。脚本还移除新环境中可能与 Torch 不匹配的 xformers；原环境不变。

SDK-only 安装只需要 aiohttp/cloudpickle：`python -m pip install /path/to/comani`。不导入 Torch 即可连接、构图并传 opaque refs；显式 fetch 某个库的对象时，本地仍需其反序列化依赖。

本机环境快照见 [environment.json](../environment.json)，历史测量见 [delivery.md](../delivery.md)。快照记录不是对任意 ComfyUI revision 或硬件的兼容保证。

## 启动与停止

```sh
.venv/bin/python -B scripts/dev_server.py start \
  --comfy-root /path/to/ComfyUI --port 8190
.venv/bin/python -B scripts/dev_server.py status
.venv/bin/python -B scripts/example.py --url http://127.0.0.1:8190
.venv/bin/python -B scripts/dev_server.py stop
```

[开发 launcher](../../scripts/dev_server.py) 固定 loopback、默认禁用 custom nodes；记录 `.local/daemon.json` 的 PID/start identity 和 `.local/daemon.log`。它拒绝占用端口，仅停止自己记录的进程。`--cpu` 可用于 CPU 图验证。

`start` 在 `/queue` 就绪检查成功后才返回，默认最多等待 120 秒，可用 `--startup-timeout` 调整。启动提前退出或超时会报告日志路径，并清理本次创建的进程；不会结束其他 daemon。Client 连接失败或启动被取消时也会关闭所有 HTTP 池和后台 sender，不依赖尚未进入的上下文执行 `__aexit__`。

## 加载新的原生节点

依照节点项目的正常方式，把 custom node 安装到指定 ComfyUI checkout，并将 Python 依赖安装到 **daemon 使用的环境**。随后用不带 `--no-custom-nodes` 的前台入口重启：

```sh
.venv/bin/python -B -m comani._runtime \
  --comfy-root /path/to/ComfyUI --host 127.0.0.1 --port 8190
```

节点注册成功后，新建 Client 会话获取 metadata，按原名调用：`remote.node("OriginalClassName", ...)`。自定义输出作为 opaque refs 传递，无需 SDK 发布新类型或生成函数。模型权重按原生节点约定放置，由 ComfyUI/model management 加载。

原生 ComfyUI 启动参数放在 `--` 后，例如追加 `-- --lowvram --reserve-vram 4`。daemon 在导入原生节点前解析并应用这些参数，具体显存策略由部署方按硬件选择。参数无效或互斥时启动失败；已初始化的 runtime 不接受事后修改启动参数。

依赖图上下文的节点放在 workflow；前端 JS 和自定义 HTTP route 不会因 registry 中有同名类就自动得到支持。安装节点依赖或改变 ComfyUI revision 后，应重新跑对应 native/集成用例。

## 远端入口

### 先部署环境与 daemon

准备一个能运行目标 ComfyUI revision 的 Python 环境，将匹配硬件的 PyTorch/CUDA、ComfyUI requirements 和 custom node 依赖安装到同一环境，再安装 wheel。SDK 客户端可使用更轻的独立环境；callable 传输的版本约束见 [Python artifacts](python-artifacts.md)。

```sh
/path/to/runtime/bin/python -m pip install /path/to/comani-0.2.0-py3-none-any.whl

# COMANI_TOKEN 已由部署环境注入；daemon 从环境读取，不写入脚本。
/path/to/runtime/bin/python -B -m comani._runtime \
  --comfy-root /path/to/ComfyUI --host 127.0.0.1 --port 8190 \
  --no-custom-nodes
```

这是普通前台启动方式，应由部署方的进程管理器管理。需要原生 custom nodes 时去掉 `--no-custom-nodes`。可以通过 SSH 转发或自己的鉴权 HTTPS 反代访问 loopback；直接绑定非 loopback 地址则必须配置 token。核心架构仍是一个 daemon 对应一个 runtime，不依赖租机平台、双隧道或验收用恢复控制面。

进程已创建、隧道已有 URL 都不等于就绪。等目标地址可达后，用真正 Client 握手确认环境可用；连接失败应让部署步骤失败或由部署方有界重试。下面检查不加载模型：

```python
import asyncio
import os
from comani import Client

async def check_ready():
    async with Client(os.environ["COMANI_URL"], token=os.environ.get("COMANI_TOKEN")) as remote:
        print("ready", remote.epoch)

asyncio.run(check_ready())
```

本次远端已验证的环境为 Python 3.11.11、cloudpickle 3.1.2、Torch 2.9.1+cu130 和固定 ComfyUI revision，详见 [smoke metadata](../../artifacts/remote/60662518-20261114/smoke/acceptance.json)。[验收 bootstrap](../../scripts/remote/bootstrap.sh) 与 [constraints](../../runtime-constraints.txt) 记录该组合；它是本次复现环境，不是所有 GPU 的通用安装器。

### 模型准备与用户例程

daemon 就绪后，再按原生节点约定下载模型和 companion weights 到 ComfyUI `models/` 下对应目录，核对文件大小与 SHA256；安装 wheel 不会下载模型。指定 H3 的三文件与目录见 [模型清单](../../scripts/remote/models.json)。实际磁盘、RAM/cgroup 和显存应单独核实，供应商报价不是模型运行的最低资源证明。

[实际 comani 例程](../../examples/minimax_h3_remote.py) 包含 eager node/Python/node、captured graph 和 raw API graph 三条链；URL/token 从环境读取。环境、模型及本地 Tensor 反序列化依赖齐备后，配置 `COMANI_URL`、`COMANI_TOKEN` 与实际 wheel 的 `COMANI_WHEEL_SHA256`，在本项目目录运行：

```sh
.venv/bin/python -B examples/minimax_h3_remote.py \
  --output artifacts/my-h3-run \
  --wheel-sha256 "$COMANI_WHEEL_SHA256"
```

该完整模型例程已在指定 INT8 checkpoint 上完成三路 2048×2048 PNG，原始 Tensor 和 PNG 逐位一致。实际部署、原生显存参数、资源测量与销毁记录统一保存在 [远端报告](../remote_acceptance.md)。

### 已验证的网络范围

真实远端 HTTPS 的 [10 项基础 smoke](../../artifacts/remote/60662518-20261114/smoke/acceptance.json)、[130 秒长任务](../../artifacts/remote/60662518-20261114/long-wait.json) 和 [NVFP4 小型 fallback 诊断](../../artifacts/remote/60662518-20261114/nvfp4-diagnostics.json) 已通过；长任务仅提交一次，发出七次结果查询。完整模型的三路出图另有 [逐位比较记录](../../artifacts/remote/60662518-20261114/image-comparison.json)。

经过带读取超时的反向代理时，服务器协商 [bounded-wait](protocol.md#经过反向代理的等待)，SDK 每轮 20 秒只读等待同一个调用，不重复提交。临时 Quick Tunnel 的 200 个在途请求上限仍适用；本次租机与传输恢复脚本只是验收设施，常规部署可选任何适当的网络入口。

## 测试

```sh
.venv/bin/python -B -m pytest -q -m 'not gpu' -p no:cacheprovider
.venv/bin/python -m ruff check src tests scripts
```

主要层次：

- [unit](../../tests/unit/)：SDK 状态、引用、打包与故障回归；native CPU fault probe 在依赖缺失时明确 skip。
- [native](../../tests/native/)：真实原生 cache、lazy/loop/expand/list 与对象接口。
- [integration](../../tests/integration/)：真实 HTTP、SDK-only 安装、GPU/model 数值链。

GPU 用例使用已启动的 daemon 和已有本地权重，按同一设备顺序执行：

```sh
COMANI_TEST_URL=http://127.0.0.1:8190 \
  .venv/bin/python -B -m pytest -q tests/integration/test_gpu.py -m 'not model'
COMANI_TEST_URL=http://127.0.0.1:8190 \
COMANI_TEST_CHECKPOINT=existing_checkpoint.safetensors \
  .venv/bin/python -B -m pytest -q tests/integration/test_gpu.py -m model
```

上述命令不下载/复制 checkpoint。保留大量 MODEL/CLIP/VAE refs 会保留 owned 对象，测试不同 pipeline 时应让不再需要的局部引用自然退出；workflow 要长期保留的结果使用显式 eager 消费者，遵循 [所有权](ownership.md)。

新增压力与远端验收由独立 agent 执行；实现者的 unit、历史 GPU 测量和文档检查不能替代其结论。
[本地独立验收](../../tests/stress/INDEPENDENT_LOAD_REPORT.md) 已通过：上述标准非 GPU suite 为 81 passed、2 deselected；原始显式 unit→integration 选择可收集 52 项；无 Torch wheel 的 15 个 Python 文件与源码、archive 一致。原 README 启动→立即示例→停止、连接失败/取消清理、pool 饱和、session churn 和完整持续负载均已独立复测通过。本地 140 秒代理验证也已通过，但不代表实际远端 HTTPS 或 GPU 模型验收。

目录提升和原生启动参数透传后的独立标准非 GPU 回归为 **89 passed、2 deselected**（25.34 秒），覆盖真实 native 与无 Torch wheel HTTP；另外通过真实 CLI 验证 `--reserve-vram 4` 已在 ComfyUI 模块初始化时生效。上段压力测量保留原始运行记录。

## 打包

```sh
.venv/bin/python -m pip wheel --no-deps --wheel-dir dist .
```

wheel 使用本目录的 `src/comani/`，发布前应在源码冻结后重建。旧项目及其环境保存在同级 `comani-old/`；停止记录的 daemon 并停止使用本仓库的 `.venv` 即可退出本地开发部署。

本仓库的干净安装验收使用独立 `.venv-wheel`。源码更新后运行 `.venv/bin/python -B scripts/refresh_wheel.py`，重建并 force reinstall 当前 wheel；脚本同时核对 source/archive/installed Python 文件 hash 和无 Torch import origin，保存 `docs/wheel_manifest.json`。只更新 editable 环境不会更新已安装 wheel，完整 suite 会检查这一点。
