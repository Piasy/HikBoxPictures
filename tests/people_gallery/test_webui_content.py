from __future__ import annotations

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from fastapi.testclient import TestClient

from hikbox_pictures.api.app import create_app

_FIXTURE_PATH = Path(__file__).with_name("fixtures_workspace.py")
_SPEC = spec_from_file_location("people_gallery_fixtures_workspace", _FIXTURE_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"无法加载测试夹具文件: {_FIXTURE_PATH}")
_MODULE = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
build_seed_workspace = _MODULE.build_seed_workspace


def test_people_page_has_cards_and_real_names(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        client = TestClient(create_app(workspace=ws.root))
        html = client.get("/").text

        assert "person-card" in html
        assert "进入维护" in html
        assert 'data-viewer-layer="original"' in html
        assert 'data-action="viewer-next"' in html
        for row in ws.person_repo.list_people():
            assert str(row["display_name"]) in html
    finally:
        ws.close()


def test_reviews_page_has_typed_queues(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        client = TestClient(create_app(workspace=ws.root))
        html = client.get("/reviews").text

        assert "queue-new_person" in html
        assert "queue-possible_merge" in html
        assert "queue-possible_split" in html
        assert "queue-low_confidence_assignment" in html
        assert 'data-action="viewer-prev"' in html
        assert 'data-action="viewer-next"' in html
        assert 'data-action="viewer-toggle-bbox"' in html
    finally:
        ws.close()


def test_sources_exports_logs_pages_bind_real_data(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        client = TestClient(create_app(workspace=ws.root))
        sources_html = client.get("/sources").text
        exports_html = client.get("/exports").text
        logs_html = client.get("/logs").text

        for source in ws.source_repo.list_sources(active=True):
            assert str(source["name"]) in sources_html
        assert "paused" in sources_html

        template = ws.export_repo.get_template(ws.export_template_id)
        assert template is not None
        assert str(template["name"]) in exports_html

        assert "seed_ready" in logs_html
    finally:
        ws.close()


def test_pages_render_empty_state_with_fresh_workspace(tmp_path) -> None:
    client = TestClient(create_app(workspace=tmp_path))

    people_html = client.get("/").text
    reviews_html = client.get("/reviews").text
    sources_html = client.get("/sources").text
    exports_html = client.get("/exports").text
    logs_html = client.get("/logs").text

    assert "人物库" in people_html
    assert "共 0 人" in people_html

    assert "待审核" in reviews_html
    assert "queue-new_person" in reviews_html

    assert "源目录与扫描" in sources_html
    assert "当前会话：idle" in sources_html
    assert "已注册源目录（0）" in sources_html

    assert "导出模板" in exports_html
    assert "输出目录" in exports_html

    assert "运行日志" in logs_html
    assert "事件类型" in logs_html
