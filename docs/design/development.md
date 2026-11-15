# 启动、部署与开发

当前项目入口是仓库根目录的 [`pyproject.toml`](../../pyproject.toml)。下面命令在 Linux/WSL 的 `comani/` 仓库根目录执行。

## 在已有环境中启动

使用部署者或模板已经配置好的 ComfyUI 运行环境：Python 3.11+、对应 backend 源码，以及其 Torch/CUDA、模型和节点依赖。进入该解释器后安装轻量 Comani 包：

```sh
python -m pip install "git+https://github.com/Delva0/comani.git"
comani daemon --comfy-root /path/to/ComfyUI -- --reserve-vram 4
```

尚未发布 PyPI 包，也可安装部署者自己的 wheel。`python -m comani daemon ...` 与 console 命令是同一入口；原有 `python -m comani._runtime ...` 保留。

CLI 直接在当前解释器中启动同一个 runtime。`--comfy-root` 选择已有源码位置，不承担 clone、更新、依赖安装、venv、版本锁定或模型下载。环境版本由部署者决定；既有源码和环境不会被启动命令重置。

daemon 默认监听 `127.0.0.1:8190`，前台运行，用 Ctrl-C/SIGTERM 或部署方进程管理器停止；没有额外 controller 或产品级 start/status/stop 服务。`--reserve-vram 4` 只是原生参数示例，按实际硬件选择。

独立客户端安装相同轻量包即可，不需要本地 Torch/ComfyUI。显式取回 Tensor 等对象时，本地仍需其反序列化依赖；callable 传输的版本约束见 [Python artifacts](python-artifacts.md)。

LZ4 是可选 extra，base 安装不依赖它。需要协商压缩时，在客户端和 daemon 环境分别安装 `"comani[lz4] @ git+https://github.com/Delva0/comani.git"`，或对本地 wheel 使用 `"/path/to/comani-0.3.0-py3-none-any.whl[lz4]"`。缺少 extra 时继续用 binary identity/兼容 JSON；`fetch_compression="identity"` 显式禁用 LZ4。配置和资源成本见 [结果取回](protocol.md#二进制结果取回)。

## 就绪与网络入口

可使用 `--host`、`--port`、`--cpu` 和 `--no-custom-nodes`；token 由 `--token` 或 `COMANI_TOKEN` 提供。非 loopback 绑定要求 token。可以使用 SSH 转发或部署方提供的鉴权 HTTPS 反代；租机、隧道、模板和恢复控制面均不属于 daemon。

进程创建或 URL 可达不等于 runtime 已就绪。使用真正 Client 完成握手，失败时让部署步骤失败或由部署方有界重试：

```python
import asyncio
import os
from comani import Client

async def check_ready():
    async with Client(os.environ["COMANI_URL"], token=os.environ.get("COMANI_TOKEN")) as remote:
        print("ready", remote.epoch)

asyncio.run(check_ready())
```

这个检查不加载模型。Client 连接失败或启动被取消时会清理 HTTP 池与后台 sender。经过带读取超时的反向代理时，SDK 协商 [bounded-wait](protocol.md#经过反向代理的等待)，每轮 20 秒只读等待同一调用，不重复提交。

## 加载新的原生节点

依照节点项目的正常方式，把 custom node 安装到选定 ComfyUI 源码目录，并将 Python 依赖安装到 **启动 daemon 的同一解释器环境**。随后用不带 `--no-custom-nodes` 的入口重启：

```sh
comani daemon \
  --comfy-root /path/to/ComfyUI --host 127.0.0.1 --port 8190
```

节点注册成功后，新建 Client 会话获取 metadata，按原名调用：`remote.node("OriginalClassName", ...)`。自定义输出作为 opaque refs 传递，无需 SDK 发布新类型或生成函数。模型权重按原生节点约定放置，由 ComfyUI/model management 加载。

原生 ComfyUI 启动参数放在 `--` 后，例如 `-- --lowvram --reserve-vram 4`。daemon 在导入原生节点前应用支持的参数；无效、互斥或 driver 明确不支持的参数会失败。它是 headless backend，不能把官方 parser 接受的所有 WebUI/server/asset 选项都当成已支持，具体边界见 [Runtime](runtime.md)。已初始化的 runtime 不接受事后修改启动参数。

依赖图上下文的节点放在 workflow；前端 JS 和自定义 HTTP route 不会因 registry 中有同名类就自动得到支持。安装节点依赖或改变 ComfyUI revision 后，应重新跑对应 native/集成用例。

## 模型与实际例程

按原生节点约定，将模型和 companion weights 放到选定 backend 的 `models/` 或额外模型路径，并核对大小与 SHA256。指定 H3 的三文件和实际目录见 [历史模型清单](https://github.com/Delva0/comani/blob/758738ee07e1d13223fde9013effd4fa6286f942/scripts/remote/models.json)。磁盘、RAM/cgroup 与显存按真实运行核实，供应商报价不是最低资源证明。

[实际 comani 例程](../../examples/minimax_h3_remote.py) 包含 eager node/Python/node、captured graph 和 raw API graph 三条链；URL/token 从环境读取。环境、模型及本地 Tensor 反序列化依赖齐备后，配置 `COMANI_URL`、`COMANI_TOKEN` 与实际 wheel 的 `COMANI_WHEEL_SHA256`，在本项目目录运行：

```sh
python -B examples/minimax_h3_remote.py \
  --output artifacts/my-h3-run \
  --wheel-sha256 "$COMANI_WHEEL_SHA256"
```

该实际脚本在 0.2.0 执行基线上完成了三路 2048×2048 PNG，原始 Tensor 和 PNG 逐位一致；本地真实来源另见 [local_checkpoint.py](../../examples/local_checkpoint.py)。[远端报告](../remote_acceptance.md) 保留其 wheel、环境、原生显存参数、资源和销毁证据，不把它改记为新 CLI 验收。

[source-file 例程](../../examples/minimax_h3_source_remote.py) 提供后续官方模板验证链；[复用例程](../../examples/minimax_h3_reuse.py) 显式保留稳定 refs，默认最多两张在途，`--serial` 使用相同 eager 调用关闭重叠。其使用步骤、真实性能及待验范围集中在 [性能记录](../performance.md)。客户端本地图片和模型上传使用 [文件 I/O](files.md)，不依赖 Python helper staging。

## 测试

以下是开发者命令，不是启动 daemon 的部署条件。先选择具有测试所需 ComfyUI/Torch 依赖的解释器，再安装测试 extras；SDK-only 用例本身不需要 native 依赖。

```sh
python -m pip install -e ".[test]"
python -B -m pytest -q -m 'not gpu' -p no:cacheprovider
python -m ruff check src tests scripts
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
[启动与保活验收](../../tests/acceptance/HEARTBEAT_REPORT.md) 绑定当前 v4-r2 原始 wheel：工程 CPU 回归 184 passed、2 deselected；独立 Python 3.12 定向回归和跨版本 source CPU 链通过。[当前压力报告](../../tests/stress/CURRENT_HEAD_PRESSURE_REPORT.md) 记录 32 sessions/4,096 calls、1,000 会话短 TTL churn、阻塞业务 loop 后 ownedRef 可用及 GC-before-close；无活跃会话意外失效，关闭中的空会话允许 TTL 兜底。

[历史压力记录](../../tests/stress/INDEPENDENT_LOAD_REPORT.md)、[885b8cd 薄 CLI CPU 验收](../../tests/acceptance/THIN_CLI_REPORT.md) 及 v2 失败各自保留原构建身份。公开 cd6 的官方模板 GPU/H3 后续独立实测已通过，见 [远端报告](../remote_acceptance.md) 与 [source-file 例程](../../examples/minimax_h3_source_remote.py)；历史数据仍按原构建解读。范围见 [交付状态](README.md#当前边界与证据)。

历史 [environment.json](../environment.json) 和 [delivery.md](../delivery.md) 是对应测量环境的快照，不是产品中的版本选择或安装策略。

`scripts/` 仅保留 [环境诊断](../../scripts/check_environment.py) 与 [wheel 刷新](../../scripts/refresh_wheel.py)，属于开发/验证工具，不是 daemon 启动条件。可复用验收器放在 [tests/acceptance](../../tests/acceptance/)，模型与历史约束分别保存在 [模型 fixture](../../tests/fixtures/minimax_h3_models.json)、[环境 fixture](../../tests/fixtures/native_runtime_constraints.txt)；daemon 不消费这些 fixture 来安装或管理环境。已退役脚本的历史来源见 [758738e 记录](../delivery.md)。

## 打包

```sh
python -m pip wheel --no-deps --wheel-dir dist .
```

wheel 使用 `src/comani/`，发布前应在源码冻结后重建。构建工具、测试环境及历史 constraints 都属于开发/验证资料，daemon 不读取它们来管理运行环境。

开发者可用 `scripts/refresh_wheel.py` 刷新本地 wheel 测试环境；只更新 editable 安装不会更新已安装 wheel。仓库现存 [docs/wheel_manifest.json](../wheel_manifest.json) 是历史 **0.2.0** 快照，不能用作当前 0.3.0 证明。

本次 Git checkpoint 仍使用版本号 `0.3.0`；本轮 files/binary/LZ4 候选构建身份见 [性能记录](../performance.md)。历史已测 v4-r2 原始 wheel 的 SHA256 为 `64a73a6e6a3c40ca6435c521bb9d7df4411e9e646c45334c863796b741babd98`。以下 [verifier](../../tests/acceptance/verify_wheel.py) 命令用于核对该历史原件：

```sh
mkdir -p .local/verification
python tests/acceptance/verify_wheel.py \
  --wheel /path/to/comani-0.3.0-py3-none-any.whl \
  --sha256 64a73a6e6a3c40ca6435c521bb9d7df4411e9e646c45334c863796b741babd98 \
  --report .local/verification/wheel.json
```

[v4-r2 生产清单](../../tests/acceptance/results/heartbeat-v4-r2/production-manifest.json) 包含 17 个 package Python 文件和 `pyproject.toml`；[实际安装核对](../../tests/acceptance/results/heartbeat-v4-r2/package-identity.json) 将当时 source/archive/独立安装绑定到同一内容。重新构建的文件应按其实际 SHA256 重新验证，不能借用已测原件的身份。

885b8cd 的 `bb117…` [基线清单](../../tests/acceptance/results/thin-cli-0.3.0/production-manifest.json) 与 [安装记录](../../tests/acceptance/results/thin-cli-0.3.0/cleanup-wheel.json) 继续作为历史证据保存。
