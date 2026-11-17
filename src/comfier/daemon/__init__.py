"""Out-of-band management and one independently owned native runtime."""

__all__ = ["NativeRuntime"]


def __getattr__(name):
    if name == "NativeRuntime":
        from .service import NativeRuntime

        return NativeRuntime
    raise AttributeError(name)
