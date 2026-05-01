from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


DEFAULT_CHECKPOINT = Path(__file__).resolve().parent / "checkpoint.pt"


class MultiHeadEfficientNet:
    def __new__(cls, models, nn, num_patterns: int, num_signals: int):
        class _Model(nn.Module):
            def __init__(self) -> None:
                super().__init__()
                self.backbone = models.efficientnet_b0(weights=None)
                feature_dim = self.backbone.classifier[1].in_features
                self.backbone.classifier = nn.Identity()
                self.pattern_head = nn.Linear(feature_dim, num_patterns)
                self.signal_head = nn.Linear(feature_dim, num_signals)

            def forward(self, images):
                features = self.backbone(images)
                return self.pattern_head(features), self.signal_head(features)

        return _Model()


def predict_chart(
    image_path: str | Path,
    checkpoint_path: str | Path = DEFAULT_CHECKPOINT,
    device: str | None = None,
) -> dict[str, Any]:
    torch, nn, Image, transforms, models = _imports()
    checkpoint_path = Path(checkpoint_path)
    checkpoint = _safe_torch_load(torch, checkpoint_path)

    pattern_classes = checkpoint["pattern_classes"]
    signal_classes = checkpoint["signal_classes"]
    selected_device = torch.device(device or ("cuda" if torch.cuda.is_available() else "cpu"))

    model = MultiHeadEfficientNet(models, nn, len(pattern_classes), len(signal_classes)).to(selected_device)
    model.load_state_dict(checkpoint["model_state_dict"])
    model.eval()

    transform = transforms.Compose(
        [
            transforms.Resize((224, 224)),
            transforms.ToTensor(),
            transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
        ]
    )

    with Image.open(image_path) as image:
        tensor = transform(image.convert("RGB")).unsqueeze(0).to(selected_device)

    with torch.no_grad():
        pattern_logits, signal_logits = model(tensor)
        pattern_probs = torch.softmax(pattern_logits, dim=1)[0]
        signal_probs = torch.softmax(signal_logits, dim=1)[0]
        pattern_id = int(pattern_probs.argmax().item())
        signal_id = int(signal_probs.argmax().item())

    pattern = pattern_classes[pattern_id]
    signal = signal_classes[signal_id]
    signal_from_pattern = signal_for_pattern(pattern)

    return {
        "detected_pattern": pattern,
        "pattern_confidence": round(float(pattern_probs[pattern_id].item()), 6),
        "signal": signal,
        "signal_confidence": round(float(signal_probs[signal_id].item()), 6),
        "signal_from_pattern_rule": signal_from_pattern,
        "heads_agree": signal == signal_from_pattern,
        "pattern_probabilities": _probability_map(pattern_classes, pattern_probs),
        "signal_probabilities": _probability_map(signal_classes, signal_probs),
    }


def signal_for_pattern(pattern: str) -> str:
    mapping = {
        "ascending_triangle": "BUY",
        "descending_triangle": "SELL",
        "double_bottom": "BUY",
        "double_top": "SELL",
        "inverse_head_and_shoulders": "BUY",
        "head_and_shoulders": "SELL",
        "bull_flag": "BUY",
        "bear_flag": "SELL",
        "no_clear_pattern": "NEUTRAL",
    }
    return mapping.get(pattern, "NEUTRAL")


def _probability_map(classes, probabilities) -> dict[str, float]:
    return {
        class_name: round(float(probabilities[index].item()), 6)
        for index, class_name in enumerate(classes)
    }


def _imports():
    try:
        import torch
        from PIL import Image
        from torch import nn
        from torchvision import models, transforms
    except ModuleNotFoundError as exc:
        raise RuntimeError("Missing dependency. Install with: python -m pip install -r requirements.txt") from exc
    return torch, nn, Image, transforms, models


def _safe_torch_load(torch, checkpoint_path: Path):
    if not checkpoint_path.exists():
        raise FileNotFoundError(f"Checkpoint not found: {checkpoint_path}")
    try:
        return torch.load(checkpoint_path, map_location="cpu", weights_only=True)
    except TypeError:
        return torch.load(checkpoint_path, map_location="cpu")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run chart pattern inference.")
    parser.add_argument("image", help="Chart image path")
    parser.add_argument("--checkpoint", default=str(DEFAULT_CHECKPOINT), help="Checkpoint .pt path")
    parser.add_argument("--device", default=None, help="cpu, cuda, or empty for auto")
    args = parser.parse_args()
    result = predict_chart(args.image, args.checkpoint, args.device)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
