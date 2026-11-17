# Python 代码与目录包

实现：[code.py](../../src/comfier/code.py)、[参数 layout](../../src/comfier/protocol.py)、[原生 Python 节点](../../src/comfier/daemon/python_node.py)。

`cf.python()` / `remote.python()` 准备代码 artifact 和参数端口，再调用 driver 注册的内部 `ComaniPython` 原生节点。默认返回一个 Output，与单输出原生节点相同；需要 Call 时显式 `return_call=True`。它仍复用调用、输出和所有权规则，不是本地执行。

## 四种显式来源

```python
import comfier as cf

# 一小段自包含 UTF-8 源码：code 是源码内容，不是路径，entry 必填
result = cf.python(
    code="def adjust(image):\n    return image * 0.5\n",
    entry="adjust", inputs={"image": image},
)

# 一个 UTF-8 文件：entry 是文件内的 callable
result = cf.python(source="adjust.py", entry="main", inputs={"image": image})

# 一个普通目录包：完整入口和相对包根的短入口均可
result = cf.python(source="my_ops", entry="my_ops.pipeline:main", inputs={"image": image})
result = cf.python(source="my_ops", entry="pipeline:main", inputs={"image": image})

# callable：本地与对端必须匹配 Python patch 版本和 cloudpickle 版本
result = cf.python(lambda value: value + 1, inputs={"value": ref})
```

function、code、source 三种参数互斥。inline/file/package 都是用户显式选择的源码，由目标解释器重新编译；不强制与客户端 Python 相同，也不改写语法、安装依赖或抓取本机 globals/闭包。目标 Python 必须支持所用语法与库 API；目录包才提供正常包内导入上下文。

Callable 使用 cloudpickle，SDK 和 daemon 检查确切 Python/cloudpickle 版本。捕获 SDK RemoteRef/Future/GraphRef 的闭包会拒绝，应把引用放进 inputs。环境不匹配不会自动翻译或 fallback 为源码，也没有自动提取任意函数依赖的 helper。

`args` 和 `inputs` 作为同一个对象图传递，保留重复对象的别名和循环；其中 SDK 引用继续成为原生依赖端口。参数中的动态闭包或动态类也使用 cloudpickle，并要求双方 Python/cloudpickle 精确版本匹配；源码模式携带普通可 pickle 数据仍允许跨 Python 版本。锁、生成器等不可序列化资源会报告明确的参数错误。代码和 pickle 的接收执行属于同一受信任代码执行边界，不是安全沙箱。

## 一份脚本包含本地入口与远端逻辑

```python
def adjust(image):
    return image * 0.5


if __name__ == "__main__":
    import asyncio
    from pathlib import Path

    import comfier as cf

    async def main():
        async with cf.Client("http://127.0.0.1:8190"):
            await cf.comfyui.start()
            image = cf.node("EmptyImage", width=64, height=64, batch_size=1, color=0)
            adjusted = cf.python(source=Path(__file__), entry="adjust", inputs={"image": image})
            saved = cf.node("SaveImage", images=adjusted,
                                filename_prefix="comfier/source", return_call=True)
            await cf.files.download(await saved.file(), "adjusted.png")

    asyncio.run(main())
```

文件 loader 执行模块级定义，main guard 内的客户端代码不在服务器运行。远端函数需要的 imports 放在模块级或函数内；仅本地用的 imports 放在 main guard，避免远端加载本地 CLI 或 example 模块。完整 H3 变体见 [source example](../../examples/minimax_h3_source_remote.py)。这不是把源文件的本地执行状态打包过去。

## 打包与内容标识

内部 CodeSpec 描述 kind、artifact ID、entrypoint、package root，以及 pickle 来源的版本信息。SHA256 标识上传的实际内容。inline 通过源码 artifact 传输，与文件源码具有相同的目标编译/依赖边界。

目录 ZIP 按 POSIX 相对路径排序，固定时间、mode、platform metadata，并使用 stored 格式，避免打包时间、chmod 或压缩器差异制造版本变化。包根目录名属于内容身份；Python 源码 bytes 保持原样。

只收集显式目录内的文件；忽略 .git/.venv/__pycache__/.pytest_cache、pyc/pyo 和 .env*，拒绝越出目录的文件 symlink。不把 SDK 环境、机器依赖或任意上级目录一并上传。

捕获 graph 时只本地准备代码和记录 artifact，不发送计算请求。提交时按内容 ID 上传复用；args/kwargs 中嵌套 refs 被提升为原生输入端口，不作为代码闭包传递。

## 正常 Python package namespace

目录名必须是有效包名；例如 my_ops/ 的短入口 pipeline:main 正规化为 my_ops.pipeline:main。包内 `from my_ops.helper import ...` 和 `from .helper import ...` 使用正常 Python import。

daemon 进程记录 top-level package name → content hash：

- 同名同 hash 复用同一个标准模块与 globals，跨调用、跨客户端 session 共享。
- 同名不同 hash 报 PackageConflict，不临时覆盖 sys.modules。
- 与已安装或已导入的其他包同名也明确冲突，不遮蔽运行时依赖。
- session close 不卸载目录包；切换同名不同版本需要新的 daemon 进程。

这不是 pip 项目构建器、依赖同步器或多版本模块沙箱。模块自己保留的 globals 遵循普通进程生命周期，不属于 Output GC 的对象所有权保证。单文件/inline 使用 loader 的 session 私有模块，不具有目录包跨 session 的标准 namespace 保证。

## 执行与结果

Python 节点可在 eager 与 graph 中使用；函数返回 awaitable 时由节点等待。所有返回值都放在一个输出槽：

```python
result = await cf.python(code="def main():\n    return (1, False, None)\n", entry="main")
assert await result.fetch() == (1, False, None)

call = cf.python(code="def main():\n    return 42\n", entry="main", return_call=True)
result, = await call
```

默认 Output 可以直接传给后续节点，不再写 .output()。保留 Call 时整体 await 仍给 refs tuple。

节点被 demand 时不复用旧 Python 计算结果，避免副作用被缓存跳过；未被 lazy 分支请求的节点不会因此强制执行。无返回值被使用但需要副作用时，在 graph 内保留 Call 并显式声明 target。

Eager 输出 owned，workflow 输出 borrowed；Python wrapper 不改变 [对象生命周期](ownership.md)。
