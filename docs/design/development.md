# 运行、示例与开发

管理和客户端需要 Python 3.11+ 与 Comani 轻量包。ComfyUI、Torch、节点和模型只需要存在于明确选定的远端计算环境。本文命令在 Linux/WSL；服务器路径与客户端路径分别标明。

## 启动管理 daemon

把当前 wheel 放到服务器并安装到管理环境：

```sh
python -m pip install /server/packages/comani-0.4.0-py3-none-any.whl
comani daemon \
  --comfy-root /server/ComfyUI \
  --workspace /server \
  --no-start
```

ComfyUI 目录可尚不存在。`python -m comani daemon` 与 console 入口相同。默认前台监听 127.0.0.1:8190；Ctrl-C/SIGTERM 或部署者进程管理器停止。公开入口统一为 `python -m comani` 与 `comani`；CLI 位于 `cli/`，单向调用 `daemon/`。

`--no-start` 选择纯管理。为兼容旧启动脚本，不加时 CLI 尝试启动计算；**Client 连接始终只管理握手**，不自动安装/启动。已有模板通过 `--runtime-python /server/venv/bin/python` 选择解释器。源码位置、workspace、硬件和原生参数显式配置，连接不迁移或重置它们。只保留必要启动入口，不实现旧 39 项 CLI 候选。

可通过 `--config /server/runtime.json` 保存/加载配置，明确 flags 覆盖配置。API configure 的持久化需要 daemon 配置了该文件；没有配置文件时修改只在该进程中有效。`--print-config` 可查看有效配置。配置字段包括 runtime_python/comfy_root/workspace/cpu/custom_nodes/comfy_args、计算宽限与配额、文件大小与传输 policy。

## 就绪与网络入口

token 取自 COMANI_TOKEN 或 --token，非 loopback 绑定必须有 token。使用部署方 SSH 转发/鉴权 HTTPS；租机、隧道、多实例调度和账号/系统安装由部署者负责。

```python
import asyncio
import os
from comani import Client


async def check_ready():
    async with Client(os.environ["COMANI_URL"], token=os.environ.get("COMANI_TOKEN")) as remote:
        info = await remote.comfyui.info()
        print(info.state, info.installed, info.runtime_python)


asyncio.run(check_ready())
```

这个检查只证明管理入口及实际状态，不加载模型，也不声称计算就绪。需要计算时显式 start，再执行对应原生调用。启动缺依赖会报 RuntimeEnvironmentIncomplete，管理/bash/files 仍存活。网络中断在配置的恢复窗口内观察原调用，不重跑脚本；显式关闭仍优先撤销计算会话。

## CPU 完整示例

从纯管理 daemon 开始，在客户端项目目录运行：

```sh
python examples/basic.py \
  --url http://127.0.0.1:8190 \
  --new-environment /server/new-comfy-env \
  --comfy-root /server/ComfyUI \
  --runtime-package /server/packages/current-comani.whl \
  --output artifacts/basic.png --stop
```

server 环境目录必须不存在，wheel 必须是服务器已有的本次精确包。脚本逐步 create_environment→configure CPU→prepare_tools→install 固定 core/Torch/requirements→prepare_runtime→start，任何失败中断后续步骤。它还演示 bash、本地文件 upload/stat/remove、eager/graph 和 SaveImage metadata→files.download；本地只需 Comani，无需 Torch/Pillow。

已有 CPU 环境省略三个准备参数即可。basic.py 不默改一个 GPU 环境的硬件配置。返回文件默认 create-only；再次运行请选新的本地 output。

basic.py 可在现有 runtime 运行时执行 bash 和文件准备，再显式确保 runtime 已启动。bash 是独立 OS 通道，不要求停机，也不持环境维护锁。专用安装/更新仍须先停止计算；`stop()` / `restart()` 默认等待计算及受管安装，显式 `wait=False` 才紧急中断相关工作后停止/尝试重启。

## H3 完整脚本

主入口 [minimax_h3_remote.py](../../examples/minimax_h3_remote.py) 以同一流程准备环境/插件、固定三权重并生成 eager、captured graph、native API 三种输出。默认将纯identity计算作为source artifact，支持客户端/目标Python不同；--callable明确选择要求Python/cloudpickle匹配的callable传输。[minimax_h3_source_remote.py](../../examples/minimax_h3_source_remote.py) 保留明确source-file入口；[source_ops](../../examples/minimax_h3_source_ops.py) 不读管理版本、文件或provenance。新流程不依赖私有model-verification文件、外部安装脚本或固定平台目录。

已有 GPU 模板的管理 daemon 先用 `--no-start`、实际 root/解释器/workspace 启动。客户端配置 COMANI_URL、COMANI_TOKEN；以下变量显式选服务器路径：

```sh
python examples/minimax_h3_remote.py \
  --prepare \
  --runtime-python "$RUNTIME_PYTHON" \
  --comfy-root "$COMFY_ROOT" \
  --runtime-package "$SERVER_COMANI_WHEEL" \
  --comfy-commit eee59185ff4f43ae2d9ad122b245619d27d9daad \
  --output artifacts/h3-new-run
```

`--prepare` 要求计算已停止；使用已有 core 时调用 update(commit)，缺失时 install(commit)。安装固定 comfy-cli/Manager 到目标环境，显式准备 runtime 包；脚本不猜测 CUDA。若模板需替换/安装 GPU 包，重复传完整 `--torch-requirement` 并给 `--torch-index-url`；新建 GPU venv 用 `--new-environment` 替代 `--runtime-python`，且必须明确这些 GPU package/index 参数。例如包版本和索引由调用方选为 TORCH_REQUIREMENT/TORCH_INDEX_URL，不在例程中硬编码硬件发行版。

可选约束 `--expected-python`、`--expected-torch` 核对 info 的实际环境；`--comfy-commit` 默认要求上述完整 commit。重复 `--plugin registry-id@version` 或 `--git-plugin repository-url@full-commit` 在准备阶段安装指定插件。H3 所需节点由选定 backend/插件提供，不假称任意旧模板都包含它们。

脚本在 daemon 解析 Civitai version3462708/file3365259 和 HF commit0bb149f5… 的 encoder/VAE，核对固定SHA，reuse时也验证内容。默认auto利用已装hub/Xet/aria2能力；`--transport http|aria2` 可明确选择。本例跨Civitai/HF，强制hf不适用于全部文件。可选 `--hub-version` 在prepare时安装一个明确版本；不自动装系统工具。

原生类别分别为 diffusion_models/text_encoders/vae；每类只有一个root时使用该唯一根，多根必须给 `--diffusion-root/--encoder-root/--vae-root`，值是服务器上的实际根绝对路径。三路获取用 asyncio.gather 形成就绪屏障，不是跨roots原子事务。安装失败或任意模型校验失败不会继续出图。

无 `--prepare` 时，脚本只检查现有环境/commit，校验复用或获取三模型，再显式启动计算。默认输出 native SaveImage 8-bit PNG 和 run.json，本地无需Torch。`--fetch-tensors` 额外获取精确float Tensor、保存.pt/诊断PNG并逐位比较三模式，客户端要预装匹配的Torch/NumPy/Pillow。fetch本身没有隐式PNG转换。

重复调用准备好的 refs 见 [minimax_h3_reuse.py](../../examples/minimax_h3_reuse.py)：使用相同管理参数，保留原 `--serial` 和默认 seeds426390/426391/426392；最多一个生成与一个fetch/save在途。此性能脚本明确依赖本地Tensor库，preparation_seconds单列准备时间。历史 --verification-path、source provenance 和外部 --monitor-source 已移除；资源采样由验收器另行完成，历史 artifacts 保持原样，不能把新代码记成已测。

### 六个文件的职责

| 文件 | 当前职责 |
| --- | --- |
| basic.py | CPU准备、管理bash/files、eager/graph、原生PNG |
| local_checkpoint.py | 现有远端checkpoint或明确本地upload，原生PNG下载 |
| minimax_h3_remote.py | 完整GPU实例准备/模型/三模式生成主入口 |
| minimax_h3_source_remote.py | 同流程的source计算变体 |
| minimax_h3_reuse.py | 保留refs、有界overlap、--serial与精确Tensor基准 |
| minimax_h3_source_ops.py | 必要的纯计算identity，不承担管理/文件功能 |

## 原生节点与参数

通过 plugins 安装明确registry版本或Git commit，依赖进入 runtime_python。停止计算后修改，显式start/restart读入新registry；磁盘安装成功不代表live导入成功。也可由部署者预装，SDK按原始节点名调用，不生成本地绑定类。

原生计算参数放在 `--` 后，例如 `-- --lowvram --reserve-vram 4`，或 configure(comfy_args=[...])。设备环境在Torch导入前应用；headless支持/拒绝项见 [Runtime](runtime.md)。前端JS、所有第三方HTTP route和官方WebUI不属于统一兼容承诺。

## 测试与打包

开发环境选择有测试所需ComfyUI/Torch的解释器；SDK-only用例不需要native依赖：

```sh
python -m pip install -e ".[test]"
python -B -m pytest -q -m 'not gpu' -p no:cacheprovider
python -m ruff check src tests scripts examples
python -m pip wheel --no-deps --wheel-dir dist .
```

unit验证SDK/故障/打包，native验证实际cache/lazy/loop/expand/list，integration验证HTTP/隔离环境/管理与计算。GPU用例通过 COMANI_TEST_URL 及显式COMANI_TEST_CHECKPOINT选已有权重，不自动下载或租机。

最终wheel必须在README和生产源码冻结后构建，核对source/archive/独立安装身份并执行新独立验收。历史 [wheel_manifest.json](../wheel_manifest.json) 是0.2.0快照；旧v4-r2、cd6或files/binary/LZ4报告各自记录其原wheel/source，不能借给新包。使用 [verify_wheel.py](../../tests/acceptance/verify_wheel.py) 时提供本次真实SHA，不复制历史SHA作为新构建证明。

scripts/只保留必要的 [环境诊断](../../scripts/check_environment.py) 与 [wheel刷新](../../scripts/refresh_wheel.py)。模型/constraints测试fixture和历史交付资料不是产品安装前提。新CPU/故障/GPU/WAN验收报告应独立记录实际覆盖与未测项，作者自测和文档AST/help检查不能代替真实远端结论。
