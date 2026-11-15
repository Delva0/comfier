"""Read native path configuration in isolation, without executing main/plugins."""

from __future__ import annotations

import ast
import contextlib
import importlib.abc
import itertools
import logging
import os
import sys
from pathlib import Path


class NoCompute(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {"torch", "nodes", "main", "cuda_malloc"}:
            raise ImportError("Path discovery cannot import native compute modules")
        return None


def configured_paths(config):
    """Apply only the target revision's native path configuration."""
    root = Path(config["comfy_root"]).expanduser().resolve()
    sys.path.insert(0, str(root))
    sys.argv = ["comani-paths", *config.get("comfy_args", [])]
    guard = NoCompute()
    sys.meta_path.insert(0, guard)
    try:
        with contextlib.redirect_stdout(sys.stderr):
            return _configure(root)
    finally:
        sys.meta_path.remove(guard)


def _configure(root):
    import comfy.options

    comfy.options.enable_args_parsing()
    import folder_paths
    import utils.extra_config
    from comfy.cli_args import args

    # Reuse only the target revision's own pure path setup function.
    # Importing main.py would initialize devices and plugin governance.
    tree = ast.parse((root / "main.py").read_text())
    function = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "apply_custom_paths"
        ),
        None,
    )
    if function is None:
        raise RuntimeError("This ComfyUI revision has no isolated native path setup function")
    namespace = {
        "args": args,
        "folder_paths": folder_paths,
        "utils": utils,
        "os": os,
        "logging": logging,
        "itertools": itertools,
        "__file__": str(root / "main.py"),
    }
    exec(
        compile(ast.Module(body=[function], type_ignores=[]), str(root / "main.py"), "exec"),
        namespace,
    )
    namespace["apply_custom_paths"]()
    if args.temp_directory:
        folder_paths.set_temp_directory(os.path.join(os.path.abspath(args.temp_directory), "temp"))
    assert "torch" not in sys.modules and "nodes" not in sys.modules and "main" not in sys.modules
    return folder_paths


def snapshot(config):
    folder_paths = configured_paths(config)
    return {
        "directories": {
            name: getattr(folder_paths, f"get_{name}_directory")()
            for name in ("input", "output", "temp")
        },
        "categories": {
            name: list(folder_paths.get_folder_paths(name))
            for name in folder_paths.folder_names_and_paths
            if name not in {"custom_nodes", "datasets"}
        },
        "custom_nodes": list(folder_paths.get_folder_paths("custom_nodes")),
        "aliases": {name: folder_paths.map_legacy(name) for name in ("clip", "unet")},
    }
