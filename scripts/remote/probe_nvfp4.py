# ruff: noqa: E402 -- ComfyUI imports require its source path and argv bootstrap first.
"""Small real Ada GPU check for Comfy's NVFP4 CPU-storage/GPU-dequant path."""
import argparse
import importlib.metadata
import json
import sys
from pathlib import Path

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--comfy-root', type=Path, required=True)
a = p.parse_args()
sys.path.insert(0, str(a.comfy_root.resolve()))
sys.argv = ['comani-nvfp4-probe']
import comfy.model_management as mm
import comfy.ops as ops
import torch
from comfy.quant_ops import QuantizedTensor

torch.manual_seed(426190)
device = torch.device('cuda:0')
# CPU packed weight is the permanent parameter, as on the offload path.
w = torch.randn(128, 256, dtype=torch.bfloat16, device='cpu')
q = QuantizedTensor.from_float(w, 'TensorCoreNVFP4Layout')
quant = json.dumps({'format': 'nvfp4'}).encode()
state = {
    'weight': q._qdata,
    'weight_scale': q._params.block_scale,
    'weight_scale_2': q._params.scale,
    'comfy_quant': torch.tensor(list(quant), dtype=torch.uint8),
}
operation = ops.mixed_precision_ops(compute_dtype=torch.bfloat16,
                                  disabled=ops.get_disabled_quant_formats(device))
layer = operation.Linear(256, 128, bias=False, device='cpu')
layer.load_state_dict(state, strict=True)
x = torch.randn(4, 256, dtype=torch.bfloat16, device=device)
torch.cuda.reset_peak_memory_stats()
with torch.inference_mode():
    out = layer(x)
    cpu_dequant = q.dequantize().to(device)
    gpu_dequant = q.to(device).dequantize()
    expected = torch.nn.functional.linear(x, gpu_dequant)
torch.cuda.synchronize()
print(json.dumps({'cpu_vs_gpu_dequant_max_abs_error': float((cpu_dequant - gpu_dequant).abs().max()),
                  'layer_vs_gpu_reference_max_abs_error': float((out - expected).abs().max())}), flush=True)
assert layer._full_precision_mm is True
assert torch.equal(out, expected), 'fallback output differs from explicit dequantized linear'
assert isinstance(layer.weight, QuantizedTensor)
assert layer.weight._qdata.device.type == 'cpu'
assert torch.isfinite(out).all()
print(json.dumps({
    'claim_boundary': 'small actual Ada tensor operation, not full-model generation',
    'device': torch.cuda.get_device_name(0),
    'capability': torch.cuda.get_device_capability(0),
    'torch': torch.__version__,
    'kitchen': importlib.metadata.version('comfy-kitchen'),
    'nvfp4_native_supported': mm.supports_nvfp4_compute(device),
    'full_precision_mm_fallback': layer._full_precision_mm,
    'permanent_weight_device': str(layer.weight._qdata.device),
    'permanent_weight_dtype': str(layer.weight._qdata.dtype),
    'permanent_weight_shape': list(layer.weight._qdata.shape),
    'output_device': str(out.device),
    'matches_explicit_dequantized_linear': True,
    'reference_backend': 'same CUDA dequantization backend as layer',
    'peak_cuda_allocated_bytes': torch.cuda.max_memory_allocated(),
}, indent=2))
