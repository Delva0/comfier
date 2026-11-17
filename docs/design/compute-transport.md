# 可选 ZMQ 计算通道

HTTP 是默认入口和默认计算通道。显式安装 `comfier[zmq]` 后，可以让本地 IPC 与远端 TCP 使用同一个计算协议、同一份 SDK 调用和同一个原生 worker。选择通道不改变节点、graph、Python 对象、owned/borrowed ref 或输出 slot 的语义，也不代表某种通道在所有任务上更快。

管理、模型下载、文件上传/下载、Range/续传和原生插件的 JSON 控制接口继续使用 HTTP。ZMQ 只承载计算 RPC 与二进制 pickle 结果；不提供文件路由或 LZ4 能力。

## 安装和启动

客户端、管理 daemon，以及选择 ZMQ 内部通道的计算解释器，都要安装当前 wheel 的 `zmq` extra：

```sh
python -m pip install '/server/packages/comfier-0.5.0-py3-none-any.whl[zmq]'
/server/comfy-env/bin/python -m pip install '/server/packages/comfier-0.5.0-py3-none-any.whl[zmq]'
```

`pyzmq>=27` 是可选依赖；普通导入和默认 HTTP 使用不加载它。下面在已准备好的 Linux/WSL 实例上开启 ZMQ。先由部署环境设置非空 `COMFIER_TOKEN`，并创建只允许部署用户访问的 `/server/run/comfier/` 目录：

```sh
comfier daemon --comfy-root /server/ComfyUI \
  --runtime-python /server/comfy-env/bin/python --workspace /server \
  --compute-transport zmq \
  --zmq-bind ipc:///server/run/comfier/compute.sock \
  --zmq-bind tcp://127.0.0.1:8191 --no-start
```

两个选项彼此独立：

| 选择 | 行为 |
| --- | --- |
| `--compute-transport http`，默认 | daemon 到 worker 的计算使用私有 HTTP |
| `--compute-transport zmq` | daemon 在随机的 `0700` 临时目录创建 IPC 通道，计算请求、启动探测与生命周期观察都走该通道 |
| `--zmq-bind ENDPOINT`，可重复 | 在管理 daemon 上额外开放 ZMQ 计算 gateway；可同时绑定 IPC/TCP，省略时不开放 |

`RuntimeConfig.compute_transport` 和配置 JSON 的 `compute_transport` 字段支持 `http|zmq`，默认 `http`；也可在计算停止时通过 `comfyui.configure(compute_transport="zmq")` 显式配置。第一版受管内部 IPC 要求 Unix；worker 仍保留私有 HTTP，用于文件和原生辅助路由。停止和启动失败都会回收私有通道；gateway 正常关闭时只删除仍属于自己的 IPC socket 文件。

`--no-start` 只开启管理服务；不加时 CLI 会尝试启动计算。HTTP `/manage` 握手包含实际绑定的 `compute_endpoints` 与公开的 CURVE server key。绑定地址是部署信息，不会自动推导 NAT、公网地址或防火墙配置；客户端应使用实际可达的主机地址。

## 一个 Client 同时使用计算和管理

```python
import asyncio
import os

import comfier as cf


async def main():
    transport = cf.ZMQTransport(
        "ipc:///server/run/comfier/compute.sock",  # 远端改为 tcp://server:8191
        token=os.environ["COMFIER_TOKEN"],
        management_url="http://127.0.0.1:8190",
    )
    async with cf.Client(transport=transport) as remote:
        await remote.comfyui.start()
        value = remote.python(code="def answer():\n    return 42\n", entry="answer")
        assert await (await value).fetch() == 42
        print((await remote.comfyui.info()).state)


asyncio.run(main())
```

`management_url` 复用同一个 token 和 HTTP 管理会话；连接先做管理握手，计算仍由显式 start 或已启动的实例提供。`remote.files` 和 `remote.comfyui.request(...)` 使用该 HTTP companion。省略它时 Client 直接连接计算 session，不能使用管理、文件和原生 JSON 控制接口；计算握手不会声称支持这些能力。

外部 HTTP Client 也能连接选择了内部 ZMQ 的 daemon：`cf.Client(http_url, token=...)` 保持不变，`/rpc` 计算请求通过内部 IPC 转发，pickle 原始字节直接返回，管理和文件路由仍由 HTTP 处理。

## 远端 TCP 和密钥

所有 ZMQ 请求都携带 token；非 loopback TCP 绑定或连接还必须使用 CURVE，并由客户端固定 server public key。服务器从 `COMFIER_ZMQ_CURVE_PUBLIC_KEY` 和 `COMFIER_ZMQ_CURVE_SECRET_KEY` 读取密钥，不通过命令行参数传递秘密；worker 不继承 gateway 的 secret key。

部署者可以用 `zmq.curve_keypair()` 生成服务器密钥，将两个 40 字符 Z85 值保存在权限为 `0600` 的环境文件或服务管理器秘密配置中，分别映射到上述变量。不要把 secret key 提供给客户端；客户端只接收可信来源的 public key。

```sh
# 运行前由部署环境设置 COMFIER_TOKEN 和两个 COMFIER_ZMQ_CURVE_* 变量。
comfier daemon --comfy-root /server/ComfyUI \
  --runtime-python /server/comfy-env/bin/python --workspace /server \
  --compute-transport zmq --zmq-bind tcp://0.0.0.0:8191 --no-start
```

客户端使用 `cf.ZMQTransport("tcp://server.example:8191", token=token, curve_server_key=public_key, management_url=https_url)`。同一个 ZMQServer 配置 CURVE 时，其所有绑定（包括 IPC）都使用该密钥；连接这些端点时均传 `curve_server_key`。CURVE 保护计算连接；HTTP companion 的远端加密仍由 HTTPS 或 SSH 转发提供。CURVE 客户端密钥由 SDK 临时生成，实际调用授权由逐请求 token 检查完成。

## 截止时间、恢复与所有权

`cf.ZMQTransport(timeout=60, max_inflight=16)` 为请求分配有限 credit；默认设置下 wait 和 bulk data 各有限额，为控制请求留出容量。请求的超时覆盖等待 credit、socket send 和接收回复；空 peer、黑洞和错误 CURVE key 都会到达截止时间。server 也限制并发数、消息大小与处理/发送等待；容量满返回可恢复的传输错误。默认请求/响应字节上限分别为 128 MiB/512 MiB，可在显式构造 transport/server 时调整。

计算 heartbeat 使用独立线程、事件循环和 ZMQ 连接，不依赖用户事件循环或结果等待连接。普通重连沿用原 session、resume token、epoch、call ID 与 submission window；丢失接纳回复时恢复观察原调用。单次 RPC 超时不撤销已经接纳的计算，也不延长服务器 lease；Client 的同 session 恢复可以跨越多个单次 RPC 截止时间。实际 worker 重启或 lease 到期后旧 ref 失效，不重新执行旧副作用。

`raw` session 在 ZMQ 上和公开 HTTP RPC 上都被拒绝；原生 prompt 辅助接口继续由 HTTP 提供。所有结果仍遵循 [所有权与恢复](ownership.md) 和 [计算协议](protocol.md) 的约束。
