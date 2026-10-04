"""把 `chinese-clip-rn50` 导出成 ONNX（**一次性开发步骤**，不在用户机器上跑）。

## 为什么需要这一步

TASK-010 选的 `OFA-Sys/chinese-clip-rn50`（Apache-2.0）在 HuggingFace 上
**只有 `clip_cn_rn50.pt`**（`cn_clip` 原始格式），不是 `transformers` 能直接加载的
`pytorch_model.bin` / `safetensors`。所以要自己导出一次。

## 导出用 torch，运行**不用**

| 环节 | 依赖 |
|---|---|
| 本脚本（开发机，跑一次） | `torch` `torchvision` `timm` `cn_clip` `onnx` |
| 插件运行时 | **`onnxruntime` `numpy` `pillow`** —— 不需要 torch |

产物写进 `--out` 目录。**该目录应当是 Core 的共享模型目录**：

```
<数据根>/models/chinese-clip-rn50/
```

模型**不随 `.xbcplugin` 包分发**，也不放插件数据目录 —— 它是 Core 级共享资源，
所有插件共用一份。插件不知道任何模型路径（路径由注册机制注入），
所以这里放好之后**不需要改任何插件配置**。

```bash
# 准备（在**仓库外的独立 venv** 里做，不要污染产品环境）
python -m venv .venv-export && .venv-export/Scripts/activate     # Windows
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
pip install --no-deps cn_clip && pip install timm six regex ftfy onnx

# 导出到共享模型目录（Windows 默认 %LOCALAPPDATA%\夏半仓工具箱\models）
python scripts/export_chinese_clip_onnx.py --out "%LOCALAPPDATA%\夏半仓工具箱\models\chinese-clip-rn50"
```

## 导出的正确性怎么保证

脚本自带校验：用同一张图、同一段中文，分别过 torch 与导出的 ONNX，
比较余弦相似度。**必须 > 0.99999**，否则退出码非 0。

> 注：`cn_clip` 依赖 `lmdb`，而它在本机 Windows 上要从源码编译。
> 但 `lmdb` 只用于**加载训练数据**，推理用不到 —— 所以用 `--no-deps` 装、
> 再单独补 `timm/six/regex/ftfy` 即可绕开编译。这是实测过的做法。
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

MODEL_REPO = "OFA-Sys/chinese-clip-rn50"
MODEL_FILE = "clip_cn_rn50.pt"
VISION_STRUCT = "RN50"
TEXT_STRUCT = "RBT3-chinese"
RESOLUTION = 224
CONTEXT_LENGTH = 52


def main() -> int:
    parser = argparse.ArgumentParser(description="导出 chinese-clip-rn50 为 ONNX")
    parser.add_argument(
        "--out", required=True,
        help="输出目录（应为 Core 共享模型目录下的 chinese-clip-rn50/）",
    )
    parser.add_argument("--opset", type=int, default=17)
    args = parser.parse_args()

    out = Path(args.out).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)

    import numpy as np
    import torch

    print(f"[1/5] 下载权重 {MODEL_REPO}/{MODEL_FILE}（Apache-2.0）")
    from huggingface_hub import hf_hub_download

    start = time.perf_counter()
    checkpoint = hf_hub_download(MODEL_REPO, MODEL_FILE)
    print(f"      {checkpoint}")
    print(f"      {Path(checkpoint).stat().st_size / 1048576:.1f} MB，"
          f"{time.perf_counter() - start:.1f}s")

    print(f"[2/5] 用 cn_clip 加载 {VISION_STRUCT}@{TEXT_STRUCT}")
    import cn_clip.clip as clip
    from cn_clip.clip.utils import _MODEL_INFO

    model, preprocess = clip.load_from_name(
        checkpoint, device="cpu",
        vision_model_name=VISION_STRUCT, text_model_name=TEXT_STRUCT,
        input_resolution=RESOLUTION,
    )
    model.eval()
    print(f"      参数量 {sum(p.numel() for p in model.parameters()) / 1e6:.1f}M")

    class VisionTower(torch.nn.Module):
        def __init__(self, inner):
            super().__init__()
            self.inner = inner

        def forward(self, pixel_values):
            return self.inner.encode_image(pixel_values)

    class TextTower(torch.nn.Module):
        def __init__(self, inner):
            super().__init__()
            self.inner = inner

        def forward(self, input_ids):
            return self.inner.encode_text(input_ids)

    print("[3/5] 导出视觉塔 + 文本塔")
    vision_path = out / "vision_model.onnx"
    text_path = out / "text_model.onnx"
    torch.onnx.export(
        VisionTower(model), (torch.randn(1, 3, RESOLUTION, RESOLUTION),), str(vision_path),
        input_names=["pixel_values"], output_names=["image_features"],
        dynamic_axes={"pixel_values": {0: "batch"}, "image_features": {0: "batch"}},
        opset_version=args.opset, dynamo=False,
    )
    torch.onnx.export(
        TextTower(model), (torch.zeros(1, CONTEXT_LENGTH, dtype=torch.long),), str(text_path),
        input_names=["input_ids"], output_names=["text_features"],
        dynamic_axes={"input_ids": {0: "batch"}, "text_features": {0: "batch"}},
        opset_version=args.opset, dynamo=False,
    )

    print("[4/5] 复制分词词表")
    import cn_clip.clip.bert_tokenizer as bert

    vocab_source = Path(bert.default_vocab())
    shutil.copy2(vocab_source, out / "vocab.txt")
    print(f"      {vocab_source} → {out / 'vocab.txt'}")

    print("[5/5] 校验 ONNX 与 torch 等价")
    import onnxruntime as ort

    vision = ort.InferenceSession(str(vision_path), providers=["CPUExecutionProvider"])
    text = ort.InferenceSession(str(text_path), providers=["CPUExecutionProvider"])

    sample = preprocess(
        __import__("PIL.Image", fromlist=["Image"]).Image.new("RGB", (320, 240), (90, 120, 200))
    ).unsqueeze(0)
    ids = clip.tokenize(["星空"], context_length=CONTEXT_LENGTH)
    with torch.no_grad():
        torch_vision = model.encode_image(sample).numpy()
        torch_text = model.encode_text(ids).numpy()
    onnx_vision = vision.run(None, {"pixel_values": sample.numpy()})[0]
    onnx_text = text.run(None, {"input_ids": ids.numpy().astype(np.int64)})[0]

    def cosine(left, right):
        left = left / np.linalg.norm(left, axis=-1, keepdims=True)
        right = right / np.linalg.norm(right, axis=-1, keepdims=True)
        return float((left * right).sum())

    vision_cos = cosine(torch_vision, onnx_vision)
    text_cos = cosine(torch_text, onnx_text)
    print(f"      视觉塔余弦 {vision_cos:.8f}")
    print(f"      文本塔余弦 {text_cos:.8f}")
    print(f"      向量维度 图像 {onnx_vision.shape[1]} / 文本 {onnx_text.shape[1]}")

    total = sum(path.stat().st_size for path in out.iterdir() if path.is_file())
    print(f"\n产物目录 {out}（合计 {total / 1048576:.1f} MB）")
    for path in sorted(out.iterdir()):
        print(f"  {path.name:<24} {path.stat().st_size / 1048576:>8.2f} MB")

    if min(vision_cos, text_cos) < 0.99999:
        print("\n❌ 校验未通过：ONNX 与 torch 不等价", file=sys.stderr)
        return 1
    print("\n✅ 校验通过：ONNX 与 torch 等价")
    print("   该目录是 Core 级共享模型目录，插件不需要任何配置就能找到它。")
    print("   用 library_status 的 image_embedding 字段确认已就绪。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
