# 0.5.0 公共 API 验收

2026-11-16，基于 `8093a3c3e22626fc1a04ca50684e2e60fd2f2a1e`（0.4.0）验收本轮 0.5.0 公共 API、示例与正常安装升级。最终完整 CPU 回归为 **557 passed、2 deselected**，包含 `submit(return_call=False)` 默认输出及 graph → eager 新故事；下述短宽限反例保留，H3/checkpoint GPU 推理未重跑。

## 构建身份

- wheel：`comani-0.5.0-py3-none-any.whl`，SHA256 `60d597327f6c6cd7f9f823be41e285fe9fcd3bfab918580ea3844c22f8a79d04`。
- 47 个生产 Python 模块的逐文件 SHA256 与 wheel、四个已安装环境一致；源码清单的 canonical SHA256 为 `708f0b588f0c009264748d495706095f7380a3727e5b1304423462a67333dc75`。六个示例清单的聚合 SHA256 为 `f748aaa76458e858d36d304d0a39f1eebf17771811649f6aa4daa0f711616943`。
- 客户端/原生 CPU 为 Python 3.11.11；源码传输另实测 3.11 客户端到 3.12.11 原生进程。验收客户端使用正常安装，无外部 `PYTHONPATH` 修补。
- 最终回归覆盖已冻结的产品代码和示例。新例最后仅修正 PNG 量化说明，算法与 README 未变；最终示例清单包含该说明字节。

## 实际结果

| 项目 | 结果 |
| --- | --- |
| 完整 CPU 回归 | **557 passed，2 deselected，251 warnings，207.94 秒**，包含默认 submit 输出、上下文隔离、所有权、图根选择、文件桥接、恢复、取消和纯 provider 查询等新旧用例。 |
| submit 默认输出 | 0/1/N 显式导出顺序、True Call、零导出仍执行、capture 组合、whole-workflow 完成屏障与早期 run[ref] 均覆盖；新增 19 个规则、4 个真实 native、1 个实际示例收集项。定向相关回归 100 passed，真实 native 与示例 6 passed。 |
| [basic.py](../examples/basic.py) | 无 Torch SDK 子进程真实上传 → LoadImage → inline Python → ImageInvert → SaveImage → download；12×8 PNG 像素 `(64,128,192)` 变为 `(223,191,159)`，上传输入已清理，实例仍运行。 |
| [graph_to_eager.py](../examples/graph_to_eager.py) | 无 Torch SDK 真实 CPU 执行 eager → graph 输入绑定与单导出 → 默认 submit Output → eager → SaveImage/download；64×64 PNG 实测 `(17,51,85)`，与原始 `(18,52,86)` 相差一级，符合浮点反转后 8-bit PNG 量化边界。 |
| [source 示例文件](../examples/minimax_h3_source_remote.py) | wheel SDK 上传这份确切源码，CPU 远端只加载 `soften`；6×4 PNG 像素 `(64,128,192)` 变为 `(32,64,96)`。未执行其中 H3 模型链。 |
| 跨 Python 源码 | 3.11 → 3.12.11 的 inline、文件 source 与 capture 真实执行；不兼容 callable ABI 明确拒绝。 |
| 24 Client 真 HTTP | 并发 eager 链、graph 输入与 submit 的结果各自正确；需要保留的下游结果先声明 owned 数据边。 |
| 默认宽限大图 | 最终代码、独立 daemon、默认 3600 秒 offline grace，8 sessions、32 张图、每图 224 节点，共 7168 个声明节点及 64 个 HTTP invocations；**60.80 秒通过**，32 个 owned 结果均为 input+448，取消单个 waiter 不影响同伴，最终 calls/objects/sessions/queue 均为 0。 |
| 此前候选并发专项 | 256 calls，0 unexpected；最终 calls/objects/leases/listeners/queue 均为 0。该数值保留此前 0.5.0 候选身份，未冒作最终 wheel 的重复执行。 |
| 正常安装升级 | 隔离 SDK 环境正常从 0.4.0 升到 0.5.0，无 force-reinstall、无 Torch、无旧 `comani.graph` 残留；四个既有环境均为 0.5.0，非 Comani 依赖版本不变。 |
| 静态检查 | src/tests/examples ruff 通过；独立验收快照的 6 个示例 CLI、29 个文档 Python 块和相对链接目标通过。 |

对应公开测试包括 [submit 返回](../tests/unit/test_submit_output_modes.py)、[原生等待边界](../tests/integration/test_submit_output_modes_native.py)、[上下文](../tests/unit/test_public_api_context.py)、[Python](../tests/unit/test_public_api_python.py)、[原生图](../tests/native/test_public_api_graph.py)、[真实示例](../tests/integration/test_public_api_examples.py)、[provider](../tests/unit/test_portable_queries.py) 与 [工具策略](../tests/unit/test_external_tool_policies.py)。完整回归的 251 条 warnings 为既有弃用/AppKey 提示；未发现未关闭 session/connector、未取回 Task 异常、销毁中的 pending Task 或未 await coroutine。

首轮曾出现 **556 passed、1 failed、2 deselected，219.49 秒**：旧 node-only 所有权测试在 0.3 秒宽限下发生心跳到期，与 submit 无关。随后使用与 Client 无关的受控存活堆独立复现，测得 GC 持 GIL 约 0.543 秒、下一心跳被严格到期拒绝；该时间来自诊断复现，并非首轮现场测量。只将该参数化测试的 fixture 宽限设为 2 秒，所有权断言和其他到期测试保留，产品默认值与到期规则未变；定向 46 项通过后，重新执行得到上述最终 557 项通过。原失败日志与因果诊断保留。

## 保留的短宽限反例

此前候选的同一 7168-node 负荷将 offline grace 压到 1.5 秒时，0.4.0/0.5.0、同进程/独立 daemon 四组均发生严格到期失败，不能报告为通过或冒作本轮最终 wheel 的重测。独立 keeper 仍须由 daemon 控制面处理心跳；宽限需要覆盖网络、重试和控制面停顿。本轮最终代码在默认 3600 秒的独立 daemon 上已重新通过上表大图负荷。

本轮保留既有一小时默认值和严格恢复身份规则，没有增加 active-request 无限续命或让晚到心跳复活过期 session。配置边界见 [所有权与恢复](design/ownership.md)。

## 证据范围

HF/Civitai/registry 查询使用可控 metadata 验证路由与凭证隔离；generic signed URL 下载使用真实 loopback HTTP。官方 CLI/Manager 只做实际 help/接口能力检查，不代表重新安装全部外部工具或覆盖所有上游版本。

H3/checkpoint GPU 推理、新 WAN 测量和新性能基准未执行；历史 GPU 图片和性能记录仍只属于其原构建。三个未被 pytest 收集的旧 live 验收脚本仅迁移显式 Call 与 raw-graph 调用形状，AST/ruff 已检查，其中 GPU/长等待场景未运行。

最终日志、独立探针、逐文件清单和升级记录保留于本地 `.local/public-api-independent-acceptance/submit-default/`（`REPORT.md`、`frozen-files.json`、`package-results.json` 等）；此前候选证据保留于其父目录。它们未提交到 Git，也不随 wheel 打包。完整 CPU 回归可在已正确安装、具备原生 ComfyUI CPU 依赖的环境复现：

```sh
python -m pytest -q -m 'not gpu and not model' --tb=short
```
