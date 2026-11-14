"""Bridge native initialization without starting ComfyUI's second HTTP/worker pair."""

import importlib
import os
import sys
from pathlib import Path

# These belong to services Comani does not host, rather than native node execution.
_SERVER_OPTIONS = {
    "listen", "port", "tls_keyfile", "tls_certfile", "enable_cors_header",
    "max_upload_size", "enable_compress_response_body", "auto_launch", "disable_auto_launch",
    "front_end_version", "front_end_root", "multi_user", "dont_print_server",
    "enable_manager", "enable_manager_legacy_ui", "disable_manager_ui", "enable_agent",
    "enable_assets", "enable_asset_hashing", "database_url", "quick_test_for_ci",
    "windows_standalone_build", "debug_hang",
}


def initialize(root, arguments, *, custom_nodes):
    from comfy.cli_args import args, parser

    rejected = []
    provided = set()
    for token in arguments:
        option = parser._parse_optional(token)
        if option is not None and option[0] is not None:
            provided.add(option[0].dest)
            if option[0].dest in _SERVER_OPTIONS:
                rejected.append(option[1])
    if rejected:
        parser.error("Comani does not host the native HTTP/UI/asset services required by: " + ", ".join(sorted(set(rejected))) + ". Use Comani --host/--port, or run the official ComfyUI server for those services.")
    if args.cache_none:
        parser.error("--cache-none uses native NullCache, which has no cache-provider support for Comani object inputs and borrowed references; choose --cache-classic, --cache-lru or --cache-ram")
    if not custom_nodes and args.whitelist_custom_nodes:
        parser.error("--no-custom-nodes conflicts with --whitelist-custom-nodes")
    # cuda_malloc can mutate the shared native args during automatic detection.
    # Only an explicit option in this boot request asks to reconfigure devices.
    device_options = provided & {
        "cuda_device", "default_device", "oneapi_device_selector", "deterministic",
        "cuda_malloc", "disable_cuda_malloc",
    }
    if device_options and "torch" in sys.modules:
        raise RuntimeError("Native device/allocator options must be set before importing torch; start a fresh process")

    # main.py has no device-initialization helper: preserve its pre-Torch ordering.
    cuda_malloc = importlib.import_module("cuda_malloc")
    if args.default_device is not None and args.cuda_device != "all":
        devices = list(range(32))
        devices.remove(args.default_device)
        devices.insert(0, args.default_device)
        os.environ["CUDA_VISIBLE_DEVICES"] = os.environ["HIP_VISIBLE_DEVICES"] = ",".join(map(str, devices))
    if args.cuda_device is not None and args.cuda_device != "all":
        os.environ["CUDA_VISIBLE_DEVICES"] = str(args.cuda_device)
        os.environ["HIP_VISIBLE_DEVICES"] = str(args.cuda_device)
        os.environ["ASCEND_RT_VISIBLE_DEVICES"] = str(args.cuda_device)
    if args.oneapi_device_selector is not None:
        os.environ["ONEAPI_DEVICE_SELECTOR"] = args.oneapi_device_selector
    if args.deterministic:
        os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")
    os.environ["TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL"] = "1"
    if "rocm" in cuda_malloc.get_torch_version_noimport():
        os.environ["OCL_SET_SVM_SIZE"] = "4194304"

    previous = sys.modules.get("main")
    if previous is not None and (not getattr(previous, "__file__", None) or Path(previous.__file__).resolve() != root / "main.py"):
        raise RuntimeError("The process already imported another main module; start a fresh process for ComfyUI")
    # The native module applies custom paths, governance/prestartup scripts and
    # backend initialization itself. Do not call start_comfyui(): we own execution.
    native = importlib.import_module("main")
    if args.temp_directory:
        native.folder_paths.set_temp_directory(os.path.join(os.path.abspath(args.temp_directory), "temp"))
    os.makedirs(native.folder_paths.get_temp_directory(), exist_ok=True)
    return native, args


def cache_configuration(args, execution, total_ram):
    # Selection mirrors native main.prompt_worker, where upstream currently keeps
    # this configuration inline rather than exposing a factory.
    kind = execution.CacheType.RAM_PRESSURE
    ram = inactive = 0
    if args.cache_classic:
        kind = execution.CacheType.CLASSIC
    elif args.cache_lru > 0:
        kind = execution.CacheType.LRU
    else:
        ram = min(10.0, max(2.0, total_ram * 0.10 / 1024.0))
        inactive = min(128.0, total_ram / 1024.0)
        if args.cache_ram:
            ram = args.cache_ram[0]
        if len(args.cache_ram) > 1:
            inactive = args.cache_ram[1]
    return kind, {"lru": args.cache_lru, "ram": ram, "ram_inactive": inactive}
