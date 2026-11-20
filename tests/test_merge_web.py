"""合库 Web 端到端测试：首次双库上传、换库、对照查看、下载、失败保护、重启。"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import main
from app.branch_sample import copy_with_overrides, create_branch_db
from app.merge import BRANCH_A, BRANCH_B, MAP_TABLE, MergeStore


@pytest.fixture()
def client(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setattr(main, "DATA_DIR", str(data_dir))
    main.SESSIONS.clear()
    main.app.state.merge_store = MergeStore(str(data_dir))
    return TestClient(main.app)


@pytest.fixture()
def branch_files(tmp_path):
    jia = tmp_path / "jia.db"
    yi = tmp_path / "yi.db"
    create_branch_db(str(jia), "甲")
    create_branch_db(str(yi), "乙")
    return jia, yi


def _upload_init(client, branch_files):
    with open(branch_files[0], "rb") as fj, open(branch_files[1], "rb") as fy:
        resp = client.post(
            "/merge/init",
            files={
                "jia_file": ("jia.db", fj, "application/x-sqlite3"),
                "yi_file": ("yi.db", fy, "application/x-sqlite3"),
            },
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/merge"


def test_workbench_entry_and_init_flow(client, branch_files):
    # 首页有合库入口
    page = client.get("/")
    assert page.status_code == 200 and "/merge" in page.text

    resp = client.get("/merge")
    assert resp.status_code == 200
    assert "首次合库" in resp.text

    _upload_init(client, branch_files)

    resp = client.get("/merge")
    assert resp.status_code == 200
    assert "jia.db" in resp.text and "yi.db" in resp.text
    assert "替换甲库" in resp.text and "替换乙库" in resp.text


def test_init_requires_both_files(client, branch_files):
    with open(branch_files[0], "rb") as fj:
        resp = client.post(
            "/merge/init",
            files={"jia_file": ("jia.db", fj, "application/x-sqlite3")},
        )
    assert resp.status_code == 400
    assert "缺少" in resp.text
    assert client.get("/merge/download", follow_redirects=False).status_code == 303


def test_download_merged_db_and_mapping(client, branch_files, tmp_path):
    _upload_init(client, branch_files)
    resp = client.get("/merge/download")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-sqlite3")
    out = tmp_path / "merged.db"
    out.write_bytes(resp.content)

    conn = sqlite3.connect(out)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    # 四表均有记录，且两室同号记录各占一行
    assert conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 6
    map_cols = {
        r[1] for r in conn.execute(f"PRAGMA table_info({MAP_TABLE})")
    }
    assert {
        "branch", "table_name", "original_id", "merged_id", "status"
    } <= map_cols
    conn.close()

    resp = client.get("/merge/mapping")
    assert resp.status_code == 200
    assert "原编号" in resp.text and "合库编号" in resp.text
    # 筛选
    resp = client.get("/merge/mapping", params={"branch": "jia", "table": "tests"})
    assert resp.status_code == 200
    assert "jia" in resp.text


def test_replace_flow_and_failed_swap_keeps_download(client, branch_files, tmp_path):
    _upload_init(client, branch_files)
    before = client.get("/merge/download").content

    # 成功换库：甲撤下读数 102
    revised = tmp_path / "jia_v2.db"
    copy_with_overrides(str(branch_files[0]), str(revised), delete_readings=(102,))
    with open(revised, "rb") as f:
        resp = client.post(
            f"/merge/replace/{BRANCH_A}",
            files={"file": ("jia_v2.db", f, "application/x-sqlite3")},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    after = client.get("/merge/download").content
    assert after != before
    # 撤下记录在对照页可见且标注已撤下
    page = client.get("/merge/mapping").text
    assert "已撤下" in page

    # 失败换库：悬空引用，页面列出相关编号且拒收
    bad = tmp_path / "jia_bad.db"
    copy_with_overrides(
        str(branch_files[0]),
        str(bad),
        extra_readings=(
            (301, 888, 777, 999, "measurement", 200.0, 47.5, "悬空"),
        ),
    )
    with open(bad, "rb") as f:
        resp = client.post(
            f"/merge/replace/{BRANCH_A}",
            files={"file": ("jia_bad.db", f, "application/x-sqlite3")},
        )
    assert resp.status_code == 400
    assert "888" in resp.text and "777" in resp.text and "999" in resp.text
    # 换库失败时仍可下载当前合库，且与失败前完全一致
    assert client.get("/merge/download").content == after


def test_unknown_branch_404(client, branch_files):
    _upload_init(client, branch_files)
    resp = client.post(
        "/merge/replace/bing",
        files={"file": ("x.db", b"x", "application/x-sqlite3")},
    )
    assert resp.status_code == 404


def test_bad_sqlite_rejected(client, branch_files, tmp_path):
    _upload_init(client, branch_files)
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a sqlite file")
    with open(bad, "rb") as f:
        resp = client.post(
            f"/merge/replace/{BRANCH_B}",
            files={"file": ("bad.db", f, "application/x-sqlite3")},
        )
    assert resp.status_code == 400
    assert "SQLite" in resp.text


def test_restart_persistence(client, branch_files, tmp_path):
    """模拟重启：重新构造 MergeStore 指向同一 DATA_DIR，状态应完整恢复。"""
    _upload_init(client, branch_files)
    data_dir = main.DATA_DIR
    main.app.state.merge_store = MergeStore(data_dir)

    resp = client.get("/merge")
    assert resp.status_code == 200
    assert "jia.db" in resp.text
    assert client.get("/merge/download").status_code == 200

    # 重启后仍可换库
    revised = tmp_path / "yi_v2.db"
    copy_with_overrides(
        str(branch_files[1]), str(revised),
        extra_readings=((301, 2, 1, None, "control", 125.0, 29.0, "乙新增"),),
    )
    with open(revised, "rb") as f:
        resp = client.post(
            f"/merge/replace/{BRANCH_B}",
            files={"file": ("yi_v2.db", f, "application/x-sqlite3")},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    db_bytes = client.get("/merge/download").content
    out = tmp_path / "after_restart.db"
    out.write_bytes(db_bytes)
    conn = sqlite3.connect(out)
    row = conn.execute(
        f"SELECT merged_id FROM {MAP_TABLE} "
        f"WHERE branch='yi' AND table_name='readings' AND original_id=301"
    ).fetchone()
    assert row is not None
    conn.close()
