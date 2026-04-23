"""产品链路使用的 MagFace embedding 推理器。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import torch

from hikbox_pictures._magface_iresnet import iresnet100

MAGFACE_GOOGLE_DRIVE_ID = "1Bd87admxOZvbIOAyTkGEntsEz3fyMt7H"


class MagFaceEmbedder:
    """MagFace embedding 推理器（官方 iResNet100 checkpoint）。"""

    def __init__(self, checkpoint_path: Path, device: str = "cpu") -> None:
        self.device = torch.device(device)
        self.model = iresnet100(num_classes=512)

        if not checkpoint_path.exists():
            self._download_checkpoint(checkpoint_path)
        checkpoint = torch.load(str(checkpoint_path), map_location=self.device)

        state_dict = checkpoint.get("state_dict", checkpoint)
        cleaned_state_dict = self._clean_state_dict(state_dict)
        missing, unexpected = self.model.load_state_dict(cleaned_state_dict, strict=False)
        if len(cleaned_state_dict) < 800:
            raise RuntimeError("MagFace checkpoint 加载字段过少，可能不是有效权重文件")
        if unexpected:
            print(f"[warn] MagFace unexpected keys: {len(unexpected)}")
        if missing:
            print(f"[warn] MagFace missing keys: {len(missing)}")

        self.model.eval()
        self.model.to(self.device)

    @staticmethod
    def _download_checkpoint(checkpoint_path: Path) -> None:
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            import gdown
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "未安装 gdown，且 MagFace 权重不存在。请先安装 gdown 或手动下载权重。"
            ) from exc
        print("MagFace checkpoint 不存在，开始自动下载...")
        gdown.download(id=MAGFACE_GOOGLE_DRIVE_ID, output=str(checkpoint_path), quiet=False)

    def _clean_state_dict(self, state_dict: dict[str, torch.Tensor]) -> dict[str, torch.Tensor]:
        model_state_dict = self.model.state_dict()
        cleaned: dict[str, torch.Tensor] = {}

        for key, value in state_dict.items():
            candidates = [
                key,
                key.removeprefix("features.module."),
                key.removeprefix("module.features."),
                key.removeprefix("features."),
                ".".join(key.split(".")[2:]) if key.startswith("features.module.") else key,
            ]
            for candidate in candidates:
                if candidate in model_state_dict and tuple(model_state_dict[candidate].shape) == tuple(value.shape):
                    cleaned[candidate] = value
                    break

        return cleaned

    def embed(self, aligned_face_bgr_112: np.ndarray) -> tuple[list[float], float]:
        tensor = torch.from_numpy(np.ascontiguousarray(aligned_face_bgr_112.transpose(2, 0, 1)))
        tensor = tensor.float().div(255.0).unsqueeze(0).to(self.device)

        with torch.no_grad():
            embedding = self.model(tensor).detach().cpu().numpy()[0]

        magface_quality = float(np.linalg.norm(embedding))
        norm = float(np.linalg.norm(embedding))
        if norm <= 1e-9:
            normalized = embedding
        else:
            normalized = embedding / norm
        return normalized.astype(float).tolist(), magface_quality
