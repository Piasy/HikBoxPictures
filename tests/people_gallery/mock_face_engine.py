from __future__ import annotations

from pathlib import Path

import numpy as np

from hikbox_pictures.deepface_engine import DetectedFace


class MockDeepFaceEngine:
    def __init__(self) -> None:
        self.model_name = "MockArcFace"
        self.detector_backend = "retinaface"
        self.distance_metric = "cosine"
        self.align = True
        self.distance_threshold = 0.35
        self.threshold_source = "explicit"
        self.model_key = f"{self.model_name}@{self.detector_backend}"

    def detect_faces(self, image_path: Path) -> list[DetectedFace]:
        image_name = image_path.name.lower()
        if "group" in image_name:
            return [
                DetectedFace(
                    bbox=(12, 58, 86, 8),
                    embedding=np.linspace(0.01, 0.32, 32, dtype=np.float32),
                ),
                DetectedFace(
                    bbox=(18, 112, 92, 64),
                    embedding=np.linspace(0.33, 0.64, 32, dtype=np.float32),
                ),
            ]
        return [
            DetectedFace(
                bbox=(10, 90, 110, 16),
                embedding=np.linspace(0.01, 0.32, 32, dtype=np.float32),
            )
        ]

    def represent_face(self, image_path: Path) -> np.ndarray:
        image_name = image_path.name.lower()
        if "obs-" in image_name and "1" in image_name:
            return np.linspace(0.11, 0.42, 32, dtype=np.float32)
        return np.linspace(0.21, 0.52, 32, dtype=np.float32)

    def distance(self, lhs, rhs) -> float:
        left = np.asarray(lhs, dtype=np.float32).reshape(-1)
        right = np.asarray(rhs, dtype=np.float32).reshape(-1)
        return float(np.linalg.norm(left - right))

    def min_distance(self, embedding, references) -> float:
        items = [np.asarray(item, dtype=np.float32).reshape(-1) for item in references]
        if not items:
            return float("inf")
        return min(self.distance(embedding, item) for item in items)

    def is_match(self, distance: float) -> bool:
        return float(distance) <= self.distance_threshold


def install_mock_face_engine(monkeypatch) -> None:
    monkeypatch.setattr(
        "hikbox_pictures.deepface_engine.DeepFaceEngine.create",
        lambda **_kwargs: MockDeepFaceEngine(),
    )
