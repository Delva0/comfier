"""Measure CPU/GPU dequant rounding separately from Comfy's CUDA layer oracle."""


def run():
    import json

    import comfy.model_management as mm
    import comfy.ops as ops
    import torch
    from comfy.quant_ops import QuantizedTensor

    torch.manual_seed(426190)
    device = torch.device('cuda:0')
    weight = torch.randn(128, 256, dtype=torch.bfloat16)
    packed = QuantizedTensor.from_float(weight, 'TensorCoreNVFP4Layout')
    config = json.dumps({'format': 'nvfp4'}).encode()
    state = {'weight': packed._qdata, 'weight_scale': packed._params.block_scale,
             'weight_scale_2': packed._params.scale,
             'comfy_quant': torch.tensor(list(config), dtype=torch.uint8)}
    operation = ops.mixed_precision_ops(compute_dtype=torch.bfloat16,
                                      disabled=ops.get_disabled_quant_formats(device))
    layer = operation.Linear(256, 128, bias=False, device='cpu')
    layer.load_state_dict(state, strict=True)
    inputs = torch.randn(4, 256, dtype=torch.bfloat16, device=device)
    with torch.inference_mode():
        output = layer(inputs)
        cpu_weight = packed.dequantize().to(device)
        gpu_weight = packed.to(device).dequantize()
        oracle = torch.nn.functional.linear(inputs, gpu_weight)
    torch.cuda.synchronize()
    torch.testing.assert_close(output, oracle, rtol=0, atol=0)

    def ordered(value):
        bits = value.view(torch.int16).to(torch.int32).bitwise_and(0xffff)
        return torch.where(bits >= 0x8000, 0xffff - bits, bits + 0x8000)

    difference = (cpu_weight.float() - gpu_weight.float()).abs()
    result = {
        'device': torch.cuda.get_device_name(0), 'capability': torch.cuda.get_device_capability(0),
        'dtype': str(output.dtype), 'nvfp4_native': mm.supports_nvfp4_compute(device),
        'fallback': layer._full_precision_mm, 'permanent_parameter_device': str(layer.weight._qdata.device),
        'cpu_gpu_dequant_max_abs_error': float(difference.max()),
        'cpu_gpu_dequant_max_relative_error': float((difference / gpu_weight.float().abs().clamp_min(1e-30)).max()),
        'cpu_gpu_dequant_max_bf16_ulp': int((ordered(cpu_weight) - ordered(gpu_weight)).abs().max()),
        'layer_cuda_oracle_max_abs_error': float((output - oracle).abs().max()),
        'layer_cuda_oracle_max_bf16_ulp': int((ordered(output) - ordered(oracle)).abs().max()),
        'cuda_allow_tf32': torch.backends.cuda.matmul.allow_tf32,
        'cudnn_allow_tf32': torch.backends.cudnn.allow_tf32,
        'float32_matmul_precision': torch.get_float32_matmul_precision(),
    }
    assert result['fallback'] and result['permanent_parameter_device'] == 'cpu'
    return result
