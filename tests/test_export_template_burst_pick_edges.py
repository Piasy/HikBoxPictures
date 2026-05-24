from __future__ import annotations

from dataclasses import fields
from datetime import datetime

from hikbox_pictures.product.export_templates import _build_visual_edge
from hikbox_pictures.product.export_templates import _connected_burst_groups
from hikbox_pictures.product.export_templates import BurstPickAsset
from hikbox_pictures.product.export_templates import BurstPickEdge
from hikbox_pictures.product.export_templates import _VisualFingerprint


def _bits(bit_count: int, hamming_from_zero: int = 0) -> tuple[int, ...]:
    return tuple(1 if index < hamming_from_zero else 0 for index in range(bit_count))


def _bits_from_indexes(bit_count: int, indexes: set[int]) -> tuple[int, ...]:
    return tuple(1 if index in indexes else 0 for index in range(bit_count))


def _blocks(*, matches_zero: bool = True) -> tuple[tuple[int, ...], ...]:
    value = 0 if matches_zero else 1
    return tuple(tuple(value for _ in range(15)) for _ in range(16))


def _mixed_blocks() -> tuple[tuple[int, ...], ...]:
    return tuple(
        tuple(0 if block_index < 8 else 1 for _ in range(15))
        for block_index in range(16)
    )


def _mostly_zero_blocks() -> tuple[tuple[int, ...], ...]:
    return tuple(
        tuple(0 if block_index < 9 else 1 for _ in range(15))
        for block_index in range(16)
    )


def _fingerprint(
    *,
    dhash_hamming_from_zero: int = 0,
    phash_hamming_from_zero: int = 0,
    center_phash_hamming_from_zero: int = 0,
    block_matches_zero: bool = True,
    global_phash_bits: tuple[int, ...] | None = None,
    center_phash_bits: tuple[int, ...] | None = None,
    block_phash_bits: tuple[tuple[int, ...], ...] | None = None,
    event_time: datetime | None = None,
    normalized_device: tuple[str, str] | None = ("apple", "iphone"),
) -> _VisualFingerprint:
    values = {
        "dhash_bits": _bits(128, dhash_hamming_from_zero),
        "global_phash_bits": global_phash_bits or _bits(63, phash_hamming_from_zero),
        "center_phash_bits": center_phash_bits or _bits(63, center_phash_hamming_from_zero),
        "block_phash_bits": block_phash_bits or _blocks(matches_zero=block_matches_zero),
        "event_time": event_time,
        "capture_time": event_time,
        "normalized_device": normalized_device,
        "width": 100,
        "height": 100,
        "file_size": 1024,
        "luminance_vector": (1.0, 0.0),
        "color_histogram": (1.0, 0.0),
    }
    return _VisualFingerprint(
        **{field.name: values[field.name] for field in fields(_VisualFingerprint)}
    )


def _asset(asset_id: int) -> BurstPickAsset:
    return BurstPickAsset(
        asset_id=asset_id,
        file_name=f"{asset_id}.jpg",
        bucket="only",
        month="2026-02",
        context_url=f"/context/{asset_id}",
        original_url=f"/original/{asset_id}",
        is_live=False,
    )


def _synthetic_edge(first_id: int, second_id: int) -> BurstPickEdge:
    return BurstPickEdge(
        asset_ids=tuple(sorted((first_id, second_id))),
        edge_type="edited_duplicate",
        confidence=0.86,
        phash_hamming=12,
        dhash_hamming=20,
        center_phash_hamming=14,
        block_match_ratio=0.50,
        capture_time_delta_seconds=None,
        normalized_device_match=None,
    )


def test_exact_duplicate_has_highest_priority_and_confidence() -> None:
    first = _fingerprint(
        event_time=datetime(2026, 2, 1, 12, 0, 0),
    )
    second = _fingerprint(
        dhash_hamming_from_zero=8,
        phash_hamming_from_zero=4,
        center_phash_hamming_from_zero=6,
        event_time=datetime(2026, 2, 1, 12, 20, 0),
    )

    edge = _build_visual_edge(79, first, 80, second)

    assert edge is not None
    assert getattr(edge, "edge_type", None) == "exact_duplicate"
    assert getattr(edge, "confidence", 0.0) >= 0.95
    assert edge.dhash_hamming == 8
    assert getattr(edge, "phash_hamming", None) == 4
    assert getattr(edge, "center_phash_hamming", None) == 6
    assert getattr(edge, "block_match_ratio", None) == 1.0
    assert edge.capture_time_delta_seconds == 1200.0
    assert edge.normalized_device_match is True


def test_edited_duplicate_uses_center_and_block_evidence_without_exact_match() -> None:
    first = _fingerprint(event_time=datetime(2026, 2, 1, 12, 0, 0))
    second = _fingerprint(
        dhash_hamming_from_zero=40,
        phash_hamming_from_zero=12,
        center_phash_hamming_from_zero=14,
        event_time=datetime(2026, 2, 1, 12, 0, 5),
    )

    edge = _build_visual_edge(11, first, 12, second)

    assert edge is not None
    assert getattr(edge, "edge_type", None) == "edited_duplicate"
    assert getattr(edge, "confidence", 0.0) >= 0.85
    assert edge.dhash_hamming == 40
    assert getattr(edge, "phash_hamming", None) == 12
    assert getattr(edge, "center_phash_hamming", None) == 14
    assert getattr(edge, "block_match_ratio", None) == 1.0


def test_burst_duplicate_strong_window_allows_missing_device_metadata() -> None:
    first = _fingerprint(
        event_time=datetime(2026, 2, 1, 12, 0, 0),
        normalized_device=None,
    )
    second = _fingerprint(
        dhash_hamming_from_zero=30,
        phash_hamming_from_zero=28,
        center_phash_hamming_from_zero=63,
        block_matches_zero=False,
        event_time=datetime(2026, 2, 1, 12, 0, 8),
        normalized_device=None,
    )

    edge = _build_visual_edge(143, first, 144, second)

    assert edge is not None
    assert getattr(edge, "edge_type", None) == "burst_duplicate"
    assert getattr(edge, "confidence", 0.0) >= 0.78
    assert edge.dhash_hamming == 30
    assert getattr(edge, "phash_hamming", None) == 28
    assert edge.capture_time_delta_seconds == 8.0
    assert edge.normalized_device_match is None


def test_short_window_burst_accepts_any_two_visual_conditions_without_phash_guard() -> None:
    first = _fingerprint(
        event_time=datetime(2026, 2, 1, 12, 0, 0),
        normalized_device=None,
    )
    second = _fingerprint(
        dhash_hamming_from_zero=30,
        phash_hamming_from_zero=63,
        center_phash_hamming_from_zero=63,
        event_time=datetime(2026, 2, 1, 12, 0, 8),
        normalized_device=None,
    )

    edge = _build_visual_edge(151, first, 152, second)

    assert edge is not None
    assert edge.edge_type == "burst_duplicate"
    assert edge.confidence >= 0.78
    assert edge.dhash_hamming == 30
    assert edge.phash_hamming == 63
    assert edge.center_phash_hamming == 63
    assert edge.block_match_ratio == 1.0
    assert edge.capture_time_delta_seconds == 8.0


def test_burst_duplicate_continuous_window_requires_stronger_visual_evidence() -> None:
    first = _fingerprint(event_time=datetime(2026, 2, 1, 12, 0, 0))
    second = _fingerprint(
        dhash_hamming_from_zero=30,
        phash_hamming_from_zero=16,
        center_phash_hamming_from_zero=63,
        block_matches_zero=False,
        event_time=datetime(2026, 2, 1, 12, 0, 30),
    )

    edge = _build_visual_edge(201, first, 202, second)

    assert edge is not None
    assert getattr(edge, "edge_type", None) == "burst_duplicate"
    assert getattr(edge, "confidence", 0.0) >= 0.82
    assert edge.capture_time_delta_seconds == 30.0


def test_same_scene_similar_does_not_create_strong_edge() -> None:
    first = _fingerprint(event_time=datetime(2026, 2, 1, 12, 0, 0))
    second = _fingerprint(
        dhash_hamming_from_zero=80,
        phash_hamming_from_zero=32,
        center_phash_hamming_from_zero=63,
        block_matches_zero=False,
        event_time=datetime(2026, 2, 1, 12, 2, 0),
    )

    edge = _build_visual_edge(301, first, 302, second)

    assert edge is None


def test_continuous_window_weak_visual_evidence_does_not_create_burst_edge() -> None:
    first = _fingerprint(event_time=datetime(2026, 2, 1, 12, 0, 0))
    second = _fingerprint(
        dhash_hamming_from_zero=30,
        phash_hamming_from_zero=28,
        center_phash_hamming_from_zero=63,
        block_matches_zero=False,
        event_time=datetime(2026, 2, 1, 12, 0, 30),
    )

    edge = _build_visual_edge(401, first, 402, second)

    assert edge is None


def test_cluster_merges_similar_edited_subgroups_without_direct_medoid_edge() -> None:
    fingerprints = {
        1: _fingerprint(
            global_phash_bits=_bits_from_indexes(63, set(range(4))),
            center_phash_bits=_bits_from_indexes(63, set(range(6))),
            block_phash_bits=_blocks(matches_zero=False),
        ),
        2: _fingerprint(),
        3: _fingerprint(
            global_phash_bits=_bits_from_indexes(63, set(range(12))),
            center_phash_bits=_bits_from_indexes(63, set(range(14))),
            block_phash_bits=_mostly_zero_blocks(),
        ),
        4: _fingerprint(
            global_phash_bits=_bits_from_indexes(63, set(range(18))),
            center_phash_bits=_bits_from_indexes(63, set(range(30))),
            block_phash_bits=_mostly_zero_blocks(),
        ),
    }
    edges = [
        edge
        for first_id, second_id in ((1, 2), (2, 3), (3, 4))
        if (edge := _build_visual_edge(first_id, fingerprints[first_id], second_id, fingerprints[second_id]))
        is not None
    ]

    groups = _connected_burst_groups(
        display_assets={asset_id: _asset(asset_id) for asset_id in fingerprints},
        fingerprints=fingerprints,
        edges=edges,
    )

    assert len(edges) == 3
    assert len(groups) == 1
    assert [asset.asset_id for asset in groups[0].assets] == [1, 2, 3, 4]


def test_cluster_keeps_sparse_large_strong_component_together() -> None:
    fingerprints = {
        asset_id: _fingerprint()
        for asset_id in range(1, 31)
    }
    edges = [
        _synthetic_edge(asset_id, asset_id + 1)
        for asset_id in range(1, 30)
    ]

    groups = _connected_burst_groups(
        display_assets={asset_id: _asset(asset_id) for asset_id in fingerprints},
        fingerprints=fingerprints,
        edges=edges,
    )

    assert len(groups) == 1
    assert [asset.asset_id for asset in groups[0].assets] == list(range(1, 31))
