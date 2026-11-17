# 原生 HTTP 控制接口

原生节点之外，部分插件通过 HTTP 管理编辑状态。例如 LongForge 的项目/场景查询和导出 trim 都是原作者注册的 HTTP 路由。

```python
async with comani.Client(url, token=token):
    await comani.comfyui.start()
    state = await comani.comfyui.request(
        "GET", "/longforge_unified/project", params={"name": "my-film"})
```

`remote.comfyui.request()` 与模块代理使用同一个 Client。路径是原生绝对路径；SDK 透明加上 `/native`，沿当前 daemon 的 HTTP 认证通道访问。query 用 `params`，JSON body 用 `json`。显式 ZMQ Client 使用其 `management_url` HTTP companion；没有 companion 时不支持此接口。

成功返回原 JSON；204 或空 body 返回 `None`，非空非 JSON 的成功响应报 `ValueError`。HTTP 失败保留 `aiohttp.ClientResponseError` 的 status/message；不自动重试或跟随重定向。写请求超时不代表未执行，调用者应先查询原生状态再决定下一步。接口拒绝外站 URL、内嵌 query/fragment、路径跳转和 Comani 管理/计算/文件保留路径。

worker 在原生启动完成后，以 aiohttp 公共 `add_subapp` 将 `PromptServer.routes` 挂到同端口 `/native`；父应用认证和原生 Request.app、路径参数、query/body、响应状态及中间件保持。挂载发生在应用冻结前。没有第二个监听端口；停止 worker 后这些路由也不可用。

这里挂载的是已注册的原生 core 与插件路由，并非只挂插件。例如原生模型列表、object_info、system_stats、上传和 view 路由可用。原生 `/prompt`、`/queue`、`/history`、`/ws`、`/interrupt`、`/free`、`/api/jobs` 及子路径被排除，Comani 的 `/rpc`、`/manage`、`/files` 也保留；所有 HTTP method 和匹配这些路径的广义插件 pattern 同样不能绕过。计算仍使用 Comani 的一个执行队列。

这不启动原生 WebUI、JS 扩展、原生 prompt worker 或另一个 PromptServer 生命周期；依赖原作者前端 URL、后台任务或独立服务的插件还需分别适配。静态媒体请使用文件 API；`request()` 的返回约定只覆盖 JSON 控制调用。

CPU 验证包含原 LongForge 7.8 的真实 GET project/scene、query、POST export_trim、原生 400、认证 401，以及 Comani RPC/prompt/files/ws 回归。真实公开 LongForgeProject/Render 的中段替换、显式恢复 trim 和导出也通过；采样/编码器/解码器用诊断边界替代，未运行 H3 GPU 推理或验收画面/音频质量。
