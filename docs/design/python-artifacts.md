# Python 代码与目录包

实现：[code.py](../../src/comani/code.py)、[参数 layout](../../src/comani/protocol.py)、[原生 Python 节点](../../src/comani/_runtime/python_node.py)。

`.python()` 准备代码 artifact 和参数端口，然后调用 driver 注册的 `ComaniPython` 原生节点。它复用 node 的 Call/输出/所有权规则，但包含代码准备与参数适配，并不只是改一个节点名称。

## 三种显式来源

```python
# callable：本地与对端必须匹配 Python patch 版本和 cloudpickle 版本
call = remote.python(lambda value: value + 1, inputs={"value": ref})

# 一个 UTF-8 文件：entry 是文件内的 callable
call = remote.python(source="adjust.py", entry="main", inputs={"image": ref})

# 一个普通目录包：完整入口和相对包根的短入口均可
call = remote.python(source="my_ops", entry="my_ops.pipeline:main", inputs={"image": ref})
call = remote.python(source="my_ops", entry="pipeline:main", inputs={"image": ref})
```

Callable 使用 cloudpickle，SDK 和 daemon 都检查确切 Python/cloudpickle 版本。捕获 SDK RemoteRef/Future/GraphRef 的闭包会拒绝，应把引用放进 inputs。环境不兼容不会自动翻译或 fallback 为源码。

File/package 是用户显式选择的自包含源码，按对端 Python 语法和库 API 编写；不强制与客户端 Python 版本相同，也不自动安装或抓取本机依赖。源码不会被改写成兼容旧环境的版本。

## 打包与内容标识

内部 CodeSpec 描述 kind、artifact ID、entrypoint、package root，以及 pickle 来源的版本信息。SHA256 标识上传的实际内容。

目录 ZIP 按 POSIX 相对路径排序，固定时间、mode、platform metadata，并使用 stored 格式，避免打包时间、chmod 或压缩器差异制造版本变化。包根目录名属于内容身份；Python 源码 bytes 保持原样。

只收集显式目录内的文件；忽略 `.git/.venv/__pycache__/.pytest_cache`、pyc/pyo 和 `.env*`，拒绝越出目录的文件 symlink。不把 SDK 环境、机器依赖或任意上级目录一并上传。

捕获 graph 时只本地准备代码和记录 artifact，不发送计算请求。提交时按内容 ID 上传复用，随后下发包含描述符的节点；args/kwargs 中嵌套 refs 被提升为原生输入端口，不作为代码闭包的一部分传递。

## 正常 Python package namespace

目录名必须是有效包名；例如 `my_ops/` 的短入口 `pipeline:main` 会正规化为 `my_ops.pipeline:main`。包内绝对导入 `from my_ops.helper import ...` 和相对导入 `from .helper import ...` 都使用正常 Python import 机制。

daemon 进程记录 top-level package name → content hash：

- 同名同 hash 复用同一个标准模块与 globals，跨调用、跨客户端 session 共享。
- 同名不同 hash 报 `PackageConflict`，不临时覆盖 `sys.modules`。
- 与已安装或已导入的其他包同名也明确冲突，不遮蔽运行时依赖。
- session close 不卸载目录包；切换同名不同版本需要新的 daemon 进程。

这不是 pip 项目构建器、依赖同步器或多版本模块沙箱。目录包模块自己保留的 globals 遵循普通进程生命周期，不属于 Output GC 自动释放的对象所有权保证。

单个源码文件使用当前 loader 的 session 私有模块，不具有目录包的跨 session 标准包 namespace 保证。

## 执行与结果

Python 节点可在 eager 与 graph 中使用；函数返回 awaitable 时由节点等待。所有返回值都放在一个输出槽：

```python
result, = await remote.python(lambda: (1, False, None))
assert await result.fetch() == (1, False, None)
```

节点被 demand 时不复用旧的 Python 计算结果，避免任意代码的副作用被缓存跳过；未被 lazy 分支请求的节点不会因这个策略强制执行。无结果被使用但仍需副作用时，graph 中显式声明 target。

Eager 输出 owned，workflow 输出 borrowed；Python wrapper 不改变 [对象生命周期](ownership.md)。
