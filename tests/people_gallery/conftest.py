from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

_MOCK_HELPER_PATH = Path(__file__).with_name("mock_face_engine.py")
_MOCK_HELPER_SPEC = spec_from_file_location("people_gallery_mock_face_engine", _MOCK_HELPER_PATH)
if _MOCK_HELPER_SPEC is None or _MOCK_HELPER_SPEC.loader is None:
    raise RuntimeError(f"无法加载 mock face engine 夹具: {_MOCK_HELPER_PATH}")
_MOCK_HELPER_MODULE = module_from_spec(_MOCK_HELPER_SPEC)
_MOCK_HELPER_SPEC.loader.exec_module(_MOCK_HELPER_MODULE)
install_mock_face_engine = _MOCK_HELPER_MODULE.install_mock_face_engine


@pytest.fixture(autouse=True)
def _mock_people_gallery_face_engine(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    if request.node.get_closest_marker("real_face_engine") is not None:
        return
    install_mock_face_engine(monkeypatch)
