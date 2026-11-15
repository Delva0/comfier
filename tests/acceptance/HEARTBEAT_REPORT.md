# Python 3.12、HTTP 续约与共享所有权验收

本次构建是版本 `0.3.0` 的 Git checkpoint。原始 wheel SHA256 为
`64a73a6e6a3c40ca6435c521bb9d7df4411e9e646c45334c863796b741babd98`；
[生产清单](results/heartbeat-v4-r2/production-manifest.json) 的 SHA256 为
`0b12ee002fb774911ee64604bbff1c1b761ce2c668b66508a2d4dab9956249a2`。
17 个 package Python 文件和 `pyproject.toml` 均已冻结。

## 证据归属与范围

| 执行方 | 实际范围与结果 | 记录 |
|---|---|---|
| 工程 owner | Python 3.11 CPU 回归 184 passed、2 GPU tests deselected；8 个新隐藏根反例在旧 v4 安装上全部失败；相关 75 项回归通过 | [工程摘要](results/heartbeat-v4-r2/engineering-summary.json) |
| 独立 Python 3.12 验收方 | 实际安装 17 文件匹配；8 个 context/queued 新 case 加 6 个必要 owner/borrowed case 共 14 passed；3.11 Client→3.12 原生 HTTP 的 source 三路 CPU Tensor 逐位一致；阻塞业务 loop 四个 TTL 期间有 12 次 heartbeat，Ref 恢复后可取回及复用 | [独立摘要](results/heartbeat-v4-r2/python312-independent-summary.json) |
| 独立压力与生命周期验收方 | 同最终 manifest 的 32 会话/4,096 calls，18,412 次实际 await、4,096 次 waiter 取消，25.024 秒；1,000 会话短 TTL churn 13.204 秒；32×224 declared-node/64 invocation 大图 54.138 秒；共享 owner、queued/非空 Context、生命周期及失败启动清理通过，无活跃 lease 意外失效。关闭中的空会话允许 TTL 兜底。 | [完整报告](../stress/CURRENT_HEAD_PRESSURE_REPORT.md)、[精简结果](../stress/CURRENT_HEAD_PRESSURE_RESULTS.json) |
| 独立增量结构审查方 | 核对 weak 控制队列、SDK-owned Context 隐藏根和 strong/weak 边；该静态结论不冒充运行验收 | [审查身份与范围](results/heartbeat-v4-r2/ownership-review.json) |
| 统一发布方 | 冻结 source、wheel archive 及独立 3.11/3.12 实际安装逐字节一致；没有重建 wheel | [身份核对](results/heartbeat-v4-r2/package-identity.json) |

以上记录保持其 CPU 验收范围。后续公开 cd6 已在官方 ComfyUI 模板完成真实 H3 GPU
三路与 130 秒 HTTPS 旅程，见 [独立远端报告](../../docs/remote_acceptance.md)；两份
[source-file 例程](../../examples/minimax_h3_source_remote.py) 在实际 GPU 运行后单独公开。
历史 758738e 图片保留其原构建身份，不借作新旅程证明。

## 修复边界

HTTP keeper 使用独立线程、event loop 和连接；暂态 HTTP 状态或截断响应不等于会话已关闭，
不会抢先阻止本来仍可成功的业务 RPC。明确的终态和完整协议错误仍拒绝复用，真实业务 RPC
的错误照常传播。不会重新握手复活已过期会话，也不会重新提交 producer。

公开 Call、未 release 的 Future/Ref owner cell 和既有 Graph 各自保留 Client 能力。
Call 没有 release API；释放全部输出不撤销仍持有的 Call/Graph。输出 release 撤销同 cell
的全部别名并交还 cell 的 Client 强边。显式 close/context exit 优先撤销整个会话。

最后公开拥有者消失时，GC 停 keeper 并安排异步 disposal；keeper 退出不表示 close RPC 已到达，
普通 HTTP pool 的清理仍需要业务 loop 进展。整个 session close/TTL 可覆盖逐项 release，
不能要求每个输出都有独立 release RPC。SDK 内部任务、回调、GC 队列与 keeper 线程使用空
Context；用户主动操作、任务和回调上下文保持不变。完整契约见
[所有权与会话](../../docs/design/ownership.md)。

独立保活不保证任意长 GIL 占用、整个进程/OS 暂停或真实断网时 lease 不失效；InProcess/custom
fallback 仍共享业务 loop。借用句柄保留会话但不 pin native cache，cache eviction 仍报 Expired。
保留或释放 lease 不承诺模型固定驻留或立即移出 GPU。

## 失败记录保留

- 885b8cd 基线在业务 loop 超过 TTL 停顿时，即使强持 ownedRef 也会失效。
- v2 在短 TTL churn 中用暂态 heartbeat 错误本地误拦真实 fetch。
- v3 把单次真实 HTTP 503 误判为终态；后续 HTTP-only 阶段的通过仅属于它自身范围。
- 旧 v4 的 queued bound method 和应用 ContextVar 复制形成隐藏 Client/Graph 根；其早期通过
  不覆盖后来发现的反例。r2 对这些入口补了确定性回归，并使用实际安装包复验。

原包、manifest、反例 JSON、日志和中间候选均保留在本地；本次只精选
[工程/失败摘要](results/heartbeat-v4-r2/engineering-summary.json) 与独立小结果公开，
不覆盖原始失败，也不提交 `.local`、环境或完整日志。关闭中的空会话允许 TTL 兜底，
不能将所有历史运行概括为零 expiry。
