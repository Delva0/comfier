"""Verify import provenance, native ComfyUI imports and a real CUDA operation."""

import argparse
import importlib.metadata
import json
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--comfy-root", type=Path, required=True)
    parser.add_argument("--cuda", action="store_true")
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    import comani

    package = Path(comani.__file__).resolve()
    if not package.is_relative_to(project / "src"):
        raise RuntimeError(f"legacy/wrong comani import: {package}")
    sys.path.insert(0, str(args.comfy_root.resolve()))
    sys.argv = ["comani-environment-check"]
    import execution
    import nodes
    import torch

    result = {
        "python": sys.version.split()[0],
        "comani_file": str(package),
        "torch_file": torch.__file__,
        "torch": torch.__version__,
        "execution_file": execution.__file__,
        "base_node_count": len(nodes.NODE_CLASS_MAPPINGS),
        "cloudpickle": importlib.metadata.version("cloudpickle"),
    }
    if args.cuda:
        x = torch.arange(9, device="cuda", dtype=torch.float32).reshape(3, 3)
        expected = [[5.0, 14.0, 23.0], [14.0, 50.0, 86.0], [23.0, 86.0, 149.0]]
        assert (x @ x.T).cpu().tolist() == expected
        result["cuda_matmul"] = "passed"
        result["device"] = torch.cuda.get_device_name(0)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
