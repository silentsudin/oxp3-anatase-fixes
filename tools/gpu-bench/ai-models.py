#!/usr/bin/env python3
"""Generate two tiny ONNX models and convert them to OpenVINO IR."""
import sys
from pathlib import Path
import numpy as np
import onnx
from onnx import TensorProto, helper, numpy_helper

out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
rng = np.random.default_rng(0)

# add: [1,1024] + const
c = rng.standard_normal((1, 1024)).astype(np.float32)
x = helper.make_tensor_value_info("x", TensorProto.FLOAT, [1, 1024])
y = helper.make_tensor_value_info("y", TensorProto.FLOAT, [1, 1024])
g = helper.make_graph([helper.make_node("Add", ["x", "c"], ["y"])], "add", [x], [y],
                      [numpy_helper.from_array(c, "c")])
m = helper.make_model(g, opset_imports=[helper.make_opsetid("", 13)], producer_name="oxp3-bench")
onnx.checker.check_model(m); onnx.save(m, out / "add.onnx")

# conv: [1,3,224,224] -> [1,8,224,224]
w = (rng.standard_normal((8, 3, 3, 3)).astype(np.float32) * 0.1)
b = np.zeros(8, dtype=np.float32)
x = helper.make_tensor_value_info("x", TensorProto.FLOAT, [1, 3, 224, 224])
y = helper.make_tensor_value_info("y", TensorProto.FLOAT, [1, 8, 224, 224])
nodes = [helper.make_node("Conv", ["x", "W", "B"], ["c"], kernel_shape=[3, 3], pads=[1, 1, 1, 1]),
         helper.make_node("Relu", ["c"], ["y"])]
g = helper.make_graph(nodes, "conv", [x], [y], [numpy_helper.from_array(w, "W"), numpy_helper.from_array(b, "B")])
m = helper.make_model(g, opset_imports=[helper.make_opsetid("", 13)], producer_name="oxp3-bench")
onnx.checker.check_model(m); onnx.save(m, out / "conv.onnx")

import openvino as ov
core = ov.Core()
for name in ("add", "conv"):
    model = core.read_model(out / f"{name}.onnx")
    ov.save_model(model, out / f"{name}.xml", compress_to_fp16=(name == "conv"))
print("models:", ", ".join(p.name for p in sorted(out.glob("*.xml"))))
