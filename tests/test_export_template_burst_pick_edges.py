from __future__ import annotations

from datetime import datetime
import math

from hikbox_pictures.product.export_templates import _build_visual_edge
from hikbox_pictures.product.export_templates import _VisualFingerprint


def _fingerprint(
    *,
    dhash_bits: tuple[int, ...],
    luminance_vector: tuple[float, ...],
    color_histogram: tuple[float, ...],
    capture_time: datetime,
) -> _VisualFingerprint:
    return _VisualFingerprint(
        dhash_bits=dhash_bits,
        luminance_vector=luminance_vector,
        color_histogram=color_histogram,
        capture_time=capture_time,
        normalized_device=("apple", "iphone"),
    )


def test_short_interval_same_device_high_color_similarity_bridges_burst_motion() -> None:
    first_bits = tuple(0 for _ in range(128))
    second_bits = tuple(1 if index < 16 else 0 for index in range(128))
    cosine = 0.906827
    first = _fingerprint(
        dhash_bits=first_bits,
        luminance_vector=(1.0, 0.0),
        color_histogram=(1.0, 0.0),
        capture_time=datetime(2026, 2, 1, 12, 0, 0),
    )
    second = _fingerprint(
        dhash_bits=second_bits,
        luminance_vector=(cosine, math.sqrt(1.0 - cosine * cosine)),
        color_histogram=(0.952229, 0.047771),
        capture_time=datetime(2026, 2, 1, 12, 0, 1),
    )

    edge = _build_visual_edge(79, first, 80, second)

    assert edge is not None
    assert edge.threshold == "metadata_assisted"
    assert edge.metadata_assisted is True
    assert edge.dhash_hamming == 16
    assert edge.capture_time_delta_seconds == 1.0
    assert edge.normalized_device_match is True


def test_same_second_high_color_similarity_bridges_stronger_burst_motion() -> None:
    first_bits = tuple(0 for _ in range(128))
    second_bits = tuple(1 if index < 20 else 0 for index in range(128))
    cosine = 0.930218
    first = _fingerprint(
        dhash_bits=first_bits,
        luminance_vector=(1.0, 0.0),
        color_histogram=(1.0, 0.0),
        capture_time=datetime(2026, 2, 1, 12, 0, 0),
    )
    second = _fingerprint(
        dhash_bits=second_bits,
        luminance_vector=(cosine, math.sqrt(1.0 - cosine * cosine)),
        color_histogram=(0.959988, 0.040012),
        capture_time=datetime(2026, 2, 1, 12, 0, 0),
    )

    edge = _build_visual_edge(143, first, 144, second)

    assert edge is not None
    assert edge.threshold == "metadata_assisted"
    assert edge.metadata_assisted is True
    assert edge.dhash_hamming == 20
    assert edge.capture_time_delta_seconds == 0.0
    assert edge.normalized_device_match is True
