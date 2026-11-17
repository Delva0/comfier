# 自动计算通道

`cf.Client(url, token=...)` 通过 HTTP 管理入口自动发现可达计算能力。默认先验证本机私有 IPC，再验证已部署的加密 TCP 端点；没有匹配且可达的候选时使用 HTTP。节点、graph、Python 对象、owned/borrowed ref 与输出 slot 的语义保持一致。选择依据是实际拓扑、认证和能力，不做逐调用测速，也不假定某个协议在所有任务上更快。

管理、模型下载、文件上传/下载、Range/续传和原生插件的 JSON 控制接口继续使用 HTTP。计算 socket 承载 RPC 与二进制 pickle 结果；远端取回结果时若已协商 HTTP LZ4，自动保留该压缩路径。结果超出 socket 帧上限时，只有只读 fetch 可以直接改用 HTTP stream。

## 安装和启动

客户端、管理 daemon 和计算解释器安装普通包即可，`pyzmq>=27` 已是基础依赖，旧 `zmq` extra 保留兼容。普通导入仍不加载 pyzmq。已存在的计算解释器若缺少该依赖，默认自动使用 HTTP；连接不会自动修改环境。

```sh
python -m pip install /server/packages/comfier-0.5.0-py3-none-any.whl
/server/comfy-env/bin/python -m pip install /server/packages/comfier-0.5.0-py3-none-any.whl
```

在 Linux/WSL 上普通启动即可。daemon 自动在随机 `0700` 临时目录创建本机 gateway，并为自己的 worker 单独创建私有 IPC；没有自动新增 TCP 监听。无 token 的本机管理入口沿用 loopback/同源限制，在管理握手中交付仅用于该 gateway 的随机凭据。

```sh
comfier daemon --comfy-root /server/ComfyUI \
  --runtime-python /server/comfy-env/bin/python --workspace /server \
  --no-start
```

部署或诊断时可以覆盖内部通道与额外端点：

| 选择 | 行为 |
| --- | --- |
| `--compute-transport auto`，默认 | 验证 worker 私有 IPC，不能使用时保留 HTTP |
| `--compute-transport http` | daemon 到 worker 的计算固定使用私有 HTTP |
| `--compute-transport zmq` | daemon 在随机的 `0700` 临时目录创建 IPC 通道，计算请求、启动探测与生命周期观察都走该通道 |
| `--zmq-bind ENDPOINT`，可重复 | 使用部署者指定的 gateway 端点，替代自动本机 gateway；远端 TCP 要求 CURVE |

`RuntimeConfig.compute_transport` 和配置 JSON 支持 `auto|http|zmq`，默认 `auto`。内部 IPC 要求 Unix；worker 始终保留私有 HTTP，用于兜底、文件和原生辅助路由。停止和启动失败都会回收私有通道；gateway 正常关闭时只删除仍属于自己的 IPC socket 文件。

`--no-start` 只开启管理服务；不加时 CLI 会尝试启动计算。HTTP `/manage` 握手包含 `compute_endpoints`、随机 gateway `server_id` 与公开 CURVE key。TCP wildcard 绑定使用管理请求的主机名和实际绑定的计算端口形成候选；它不推导 NAT 端口映射或修改防火墙。Client 只创建一次 canonical HTTP session，再用无 session 副作用的 `runtime_info` 探测候选；必须同时匹配 gateway identity 和 runtime epoch。IPC 还需是当前用户可见的真实 socket，且直接父目录私有。TCP 自动选择要求 CURVE pin；不可达候选有界退回 HTTP。

## 一个 Client 同时使用计算和管理

```python
import asyncio
import os

import comfier as cf


async def main():
    async with cf.Client("http://127.0.0.1:8190", token=os.environ.get("COMFIER_TOKEN")) as remote:
        await remote.comfyui.start()
        value = remote.python(code="def answer():\n    return 42\n", entry="answer")
        assert await (await value).fetch() == 42
        print((await remote.comfyui.info()).state)


asyncio.run(main())
```

计算仍由显式 start 或已启动的实例提供。`remote.files` 和 `remote.comfyui.request(...)` 复用 HTTP 管理入口。诊断时仍可显式传入 `cf.HTTPTransport(...)` 或 `cf.ZMQTransport(..., management_url=...)`；后者省略 HTTP companion 时不能使用管理、文件和原生 JSON 控制接口。

`remote.transport.selected_transport`、`compute_endpoint` 和 `selection_reason` 可用于诊断当前自动路径；`comfyui.info()` 的 `compute_channel`、`compute_fallback` 反映 worker 通道。应用调用无需读取这些诊断字段。

## 远端 TCP 和密钥

所有 ZMQ 请求都携带 token；非 loopback TCP 绑定或连接还必须使用 CURVE，并由客户端固定 server public key。服务器从 `COMFIER_ZMQ_CURVE_PUBLIC_KEY` 和 `COMFIER_ZMQ_CURVE_SECRET_KEY` 读取密钥，不通过命令行参数传递秘密；worker 不继承 gateway 的 secret key。

部署者可以用 `zmq.curve_keypair()` 生成服务器密钥，将两个 40 字符 Z85 值保存在权限为 `0600` 的环境文件或服务管理器秘密配置中，分别映射到上述变量。不要把 secret key 提供给客户端；客户端只接收可信来源的 public key。

```sh
# 运行前由部署环境设置 COMFIER_TOKEN 和两个 COMFIER_ZMQ_CURVE_* 变量。
comfier daemon --comfy-root /server/ComfyUI \
  --runtime-python /server/comfy-env/bin/python --workspace /server \
  --compute-transport zmq --zmq-bind tcp://0.0.0.0:8191 --no-start
```

普通客户端仍使用 `cf.Client(https_url, token=token)`；可信管理握手广告的端点和 public key 通过认证、可达性与 epoch 检查后自动使用。显式 `cf.ZMQTransport("tcp://server.example:8191", token=token, curve_server_key=public_key, management_url=https_url)` 保留用于诊断。同一个 ZMQServer 配置 CURVE 时，其所有绑定都使用该密钥。CURVE 保护计算连接，管理入口加密仍由 HTTPS 或 SSH 转发提供。

## 截止时间、恢复与所有权

`cf.ZMQTransport(timeout=60, max_inflight=16)` 为请求分配有限 credit；默认设置下 wait 和 bulk data 各有限额，为控制请求留出容量。请求的超时覆盖等待 credit、socket send 和接收回复；空 peer、黑洞和错误 CURVE key 都会到达截止时间。server 也限制并发数、消息大小与处理/发送等待；容量满返回可恢复的传输错误。默认请求/响应字节上限分别为 128 MiB/512 MiB，可在显式构造 transport/server 时调整。

默认 Client 的 heartbeat 使用独立线程、事件循环和 HTTP 连接，所选计算 socket 的阻塞不会阻止同一 session 续约；显式 ZMQTransport 使用独立 ZMQ keeper。自动计算连接失联只切换路径并上报传输不确定性，再由既有恢复机制通过 HTTP resume 原 session、resume token 和 epoch，重试原 call ID 与 submission window；不另建 session，不盲重发未知提交。单次 RPC 超时不撤销已接纳计算。实际 worker 重启或 lease 到期后旧 ref 失效，不重新执行旧副作用。

`raw` session 在 ZMQ 上和公开 HTTP RPC 上都被拒绝；原生 prompt 辅助接口继续由 HTTP 提供。所有结果仍遵循 [所有权与恢复](ownership.md) 和 [计算协议](protocol.md) 的约束。
