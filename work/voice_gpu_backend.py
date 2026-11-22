"""Process-local equivalent PyTorch CUDA backend for the existing MDX model.

Validates one full-size spectrum against original CPU ONNX before accepting it.
Does not modify installed packages or alter the model/segmentation parameters.
"""
from pathlib import Path
import json
import time

def enable(report_path):
    import torch
    import onnx
    import onnx2torch
    import onnxruntime as ort
    import numpy as np
    from audio_separator.separator.architectures.mdx_separator import MDXSeparator
    original=MDXSeparator.load_model
    def load(self):
        if not torch.cuda.is_available():return original(self)
        model=onnx2torch.convert(onnx.load(self.model_path)).to('cuda').eval()
        torch.backends.cuda.matmul.allow_tf32=False
        torch.backends.cudnn.allow_tf32=False
        options=ort.SessionOptions();options.intra_op_num_threads=4
        session=ort.InferenceSession(self.model_path,providers=['CPUExecutionProvider'],sess_options=options)
        generator=np.random.default_rng(314159)
        sample=generator.normal(0,.1,(1,4,self.dim_f,self.segment_size)).astype('float32')
        begin=time.monotonic();expected=session.run(None,{'input':sample})[0];cpu_s=time.monotonic()-begin
        tensor=torch.from_numpy(sample).to('cuda')
        with torch.inference_mode():
            model(tensor);torch.cuda.synchronize();begin=time.monotonic()
            actual=model(tensor);torch.cuda.synchronize();gpu_s=time.monotonic()-begin
            actual=actual.cpu().numpy()
        difference=float(np.max(np.abs(expected-actual)))
        accepted=bool(np.allclose(expected,actual,atol=5e-4,rtol=5e-4))
        Path(report_path).write_text(json.dumps({'backend':'onnx2torch CUDA float32',
            'model':self.model_path,'input_shape':list(sample.shape),'max_absolute_difference':difference,
            'equivalence_passed':accepted,'cpu_seconds':cpu_s,'gpu_seconds':gpu_s,
            'gpu':torch.cuda.get_device_name(0),'segment_size':self.segment_size},indent=2),encoding='utf8')
        if not accepted:raise RuntimeError('CUDA backend equivalence check failed')
        self.torch_device=torch.device('cuda')
        def run(spectrum):
            with torch.inference_mode():return model(spectrum).cpu().numpy()
        self.model_run=run
    MDXSeparator.load_model=load
