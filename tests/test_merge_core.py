"""合库核心逻辑测试。

覆盖需求点：同号分室独立编号、撤下/补回编号稳定、编号不复用、
换库失败保护（悬空拒收且旧合库不动）、两边先后换库均生效、重启持久化。
"""
from __future__ import annotations

import os
import sqlite3

import pytest

from app.branch_sample import copy_with_overrides, create_branch_db
from app.merge import (
    BRANCH_A,
    BRANCH_B,
    MAP_TABLE,
    META_TABLE,
    MergeError,
    MergeStore,
    validate_branch,
)
from app.core import EXPORT_TABLES


@pytest.fixture()
def store(tmp_path):
    return MergeStore(str(tmp_path / "data"))


@pytest.fixture()
def branch_dbs(tmp_path):
    jia = tmp_path / "jia.db"
    yi = tmp_path / "yi.db"
    create_branch_db(str(jia), "甲")
    create_branch_db(str(yi), "乙")
    return str(jia), str(yi)


def _init(store, branch_dbs):
    jia, yi = branch_dbs
    store.initialize(
        {
            BRANCH_A: ("jia.db", open(jia, "rb").read()),
            BRANCH_B: ("yi.db", open(yi, "rb").read()),
        }
    )


def _conn(store):
    return sqlite3.connect(store.merged_path)


def test_initial_merge_same_original_ids_get_distinct_merged_ids(store, branch_dbs):
    """同号自测：两库 readings 原编号同为 101/102/201，合库编号必须各自独立。"""
    _init(store, branch_dbs)
    conn = _conn(store)
    rows = conn.execute(
        f"SELECT branch, original_id, merged_id FROM {MAP_TABLE} "
        f"WHERE table_name='readings' ORDER BY branch, original_id"
    ).fetchall()
    merged = {(b, oid): mid for b, oid, mid in rows}
    # 各分室原编号都存在
    assert {oid for b, oid, _ in rows} == {101, 102, 201}
    # 同号记录在两个分室拿到不同的合库编号
    for oid in (101, 102, 201):
        assert merged[(BRANCH_A, oid)] != merged[(BRANCH_B, oid)]
    # 合库编号在表内唯一，绝不转给别的记录
    all_ids = [mid for _b, _o, mid in rows]
    assert len(all_ids) == len(set(all_ids))
    # 数据表保留四表记录，数量等于两库在库对照之和
    for table in EXPORT_TABLES:
        in_table = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        in_map = conn.execute(
            f"SELECT COUNT(*) FROM {MAP_TABLE} "
            f"WHERE table_name=? AND status='present'",
            (table,),
        ).fetchone()[0]
        assert in_table == in_map
    conn.close()


def test_references_resolved_within_each_branch(store, branch_dbs):
    """分组/试验归属/对照/校准引用都重写到本分室记录的合库编号，不跨室串扰。"""
    _init(store, branch_dbs)
    conn = _conn(store)
    # 外键完整性
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    mapping = {
        (b, t, oid): mid
        for b, t, oid, mid, _s in conn.execute(
            f"SELECT branch, table_name, original_id, merged_id, status "
            f"FROM {MAP_TABLE}"
        )
    }
    # readings.control_reading_id 必须指向同室原编号 101 对应的合库编号
    for branch in (BRANCH_A, BRANCH_B):
        r102 = conn.execute(
            f"SELECT r.control_reading_id FROM readings r "
            f"JOIN {MAP_TABLE} m ON m.merged_id = r.id "
            f"WHERE m.branch=? AND m.original_id=102 AND m.table_name='readings'",
            (branch,),
        ).fetchone()
        assert r102[0] == mapping[(branch, "readings", 101)]
        # 试验归属：原 test 1 → 同室合库试验
        owner = conn.execute(
            "SELECT test_id FROM readings WHERE id = ?",
            (mapping[(branch, "readings", 101)],),
        ).fetchone()[0]
        assert owner == mapping[(branch, "tests", 1)]
        # 组归属
        gid = conn.execute(
            "SELECT group_id FROM tests WHERE id = ?",
            (mapping[(branch, "tests", 1)],),
        ).fetchone()[0]
        assert gid == mapping[(branch, "groups", 1)]
        # 校准引用
        cid = conn.execute(
            "SELECT calibration_id FROM tests WHERE id = ?",
            (mapping[(branch, "tests", 1)],),
        ).fetchone()[0]
        assert cid == mapping[(branch, "calibrations", 1)]
    conn.close()


def test_withdraw_then_readd_keeps_merged_id(store, branch_dbs, tmp_path):
    """撤下后补回：记录移出数据表、对照保留；补回沿用原合库编号。"""
    _init(store, branch_dbs)
    conn = _conn(store)
    before = conn.execute(
        f"SELECT merged_id FROM {MAP_TABLE} "
        f"WHERE branch=? AND table_name='readings' AND original_id=102",
        (BRANCH_A,),
    ).fetchone()[0]
    conn.close()

    # 修订甲库：撤下读数 102
    revised = tmp_path / "jia_v2.db"
    copy_with_overrides(branch_dbs[0], str(revised), delete_readings=(102,))
    store.replace_branch(BRANCH_A, "jia_v2.db", revised.read_bytes())

    conn = _conn(store)
    assert before not in {r[0] for r in conn.execute("SELECT id FROM readings")}
    row = conn.execute(
        f"SELECT merged_id, status FROM {MAP_TABLE} "
        f"WHERE branch=? AND table_name='readings' AND original_id=102",
        (BRANCH_A,),
    ).fetchone()
    assert row == (before, "withdrawn")
    conn.close()

    # 再次换库：读数 102 补回
    store.replace_branch(BRANCH_A, "jia_v3.db", open(branch_dbs[0], "rb").read())
    conn = _conn(store)
    row = conn.execute(
        f"SELECT merged_id, status FROM {MAP_TABLE} "
        f"WHERE branch=? AND table_name='readings' AND original_id=102",
        (BRANCH_A,),
    ).fetchone()
    assert row == (before, "present")
    assert before in {r[0] for r in conn.execute("SELECT id FROM readings")}
    conn.close()


def test_withdrawn_merged_id_never_reassigned(store, branch_dbs, tmp_path):
    """新记录不得占用撤下记录的合库编号：新原编号 301 应拿到全新编号。"""
    _init(store, branch_dbs)
    conn = _conn(store)
    withdrawn_mid = conn.execute(
        f"SELECT merged_id FROM {MAP_TABLE} "
        f"WHERE branch=? AND table_name='readings' AND original_id=102",
        (BRANCH_A,),
    ).fetchone()[0]
    conn.close()

    revised = tmp_path / "jia_v2.db"
    # 撤下 102，追加全新原编号 301（引用与归属均合法）
    copy_with_overrides(
        branch_dbs[0],
        str(revised),
        delete_readings=(102,),
        extra_readings=(
            (301, 1, 1, None, "measurement", 200.0, 47.5, "新增"),
        ),
    )
    store.replace_branch(BRANCH_A, "jia_v2.db", revised.read_bytes())

    conn = _conn(store)
    new_mid = conn.execute(
        f"SELECT merged_id FROM {MAP_TABLE} "
        f"WHERE branch=? AND table_name='readings' AND original_id=301",
        (BRANCH_A,),
    ).fetchone()[0]
    assert new_mid != withdrawn_mid
    # 撤下编号仍然挂在 102 名下（同表内合库编号唯一，不会转给其他记录）
    assert conn.execute(
        f"SELECT original_id FROM {MAP_TABLE} "
        f"WHERE branch=? AND table_name='readings' AND merged_id=?",
        (BRANCH_A, withdrawn_mid),
    ).fetchone()[0] == 102
    conn.close()


def test_dangling_reference_rejected_with_ids_and_old_merged_intact(
    store, branch_dbs, tmp_path
):
    """失败换库：新版有悬空引用时报出相关编号并拒收，旧合库与对照原样保留。"""
    _init(store, branch_dbs)
    conn = _conn(store)
    snapshot = conn.execute(
        f"SELECT branch, table_name, original_id, merged_id, status "
        f"FROM {MAP_TABLE} ORDER BY 1,2,3,4"
    ).fetchall()
    merged_bytes = open(store.merged_path, "rb").read()
    conn.close()

    bad = tmp_path / "jia_bad.db"
    # 读数 301 引用不存在的对照读数 999、不存在的试验 888
    copy_with_overrides(
        branch_dbs[0],
        str(bad),
        extra_readings=(
            (301, 888, 777, 999, "measurement", 200.0, 47.5, "悬空"),
        ),
    )
    v = validate_branch(str(bad))
    assert not v.ok
    dangling = {(d.table, d.column, d.target_id) for d in v.dangling}
    assert ("readings", "test_id", 888) in dangling
    assert ("readings", "calibration_id", 777) in dangling
    assert ("readings", "control_reading_id", 999) in dangling

    with pytest.raises(MergeError) as exc:
        store.replace_branch(BRANCH_A, "jia_bad.db", bad.read_bytes())
    msg = str(exc.value)
    assert "888" in msg and "777" in msg and "999" in msg

    # 旧合库仍可下载且字节未变，编号对照原样
    assert open(store.merged_path, "rb").read() == merged_bytes
    conn = _conn(store)
    now = conn.execute(
        f"SELECT branch, table_name, original_id, merged_id, status "
        f"FROM {MAP_TABLE} ORDER BY 1,2,3,4"
    ).fetchall()
    assert now == snapshot
    # 甲分室来源文件也未被替换
    assert conn.execute(
        f"SELECT source_name FROM {META_TABLE} WHERE branch=?", (BRANCH_A,)
    ).fetchone()[0] == "jia.db"
    conn.close()


def test_replace_both_branches_sequentially_both_updates_land(
    store, branch_dbs, tmp_path
):
    """两边同时换库（先后两次请求）：成功的更新都纳入合库。"""
    _init(store, branch_dbs)
    jia_v2 = tmp_path / "jia_v2.db"
    yi_v2 = tmp_path / "yi_v2.db"
    copy_with_overrides(
        branch_dbs[0], str(jia_v2),
        extra_readings=((301, 1, 1, None, "measurement", 200.0, 47.0, "甲新增"),),
    )
    copy_with_overrides(
        branch_dbs[1], str(yi_v2),
        extra_readings=((301, 2, 1, None, "control", 125.0, 29.0, "乙新增"),),
    )
    store.replace_branch(BRANCH_A, "jia_v2.db", jia_v2.read_bytes())
    store.replace_branch(BRANCH_B, "yi_v2.db", yi_v2.read_bytes())

    conn = _conn(store)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    for branch in (BRANCH_A, BRANCH_B):
        mid = conn.execute(
            f"SELECT merged_id FROM {MAP_TABLE} "
            f"WHERE branch=? AND table_name='readings' AND original_id=301",
            (branch,),
        ).fetchone()[0]
        assert mid in {r[0] for r in conn.execute("SELECT id FROM readings")}
    # 两室新增的同号 301 仍然各自独立
    ids = [
        r[0]
        for r in conn.execute(
            f"SELECT merged_id FROM {MAP_TABLE} "
            f"WHERE table_name='readings' AND original_id=301"
        )
    ]
    assert len(ids) == 2 and ids[0] != ids[1]
    conn.close()


def test_state_survives_restart(store, branch_dbs, tmp_path):
    """重启后仍可下载和换库：新建同路径 MergeStore 即恢复全部状态。"""
    _init(store, branch_dbs)
    restarted = MergeStore(store.root.rsplit("/", 1)[0])
    assert restarted.is_initialized()
    status = restarted.status()
    assert status.initialized
    assert set(status.metas) == {BRANCH_A, BRANCH_B}
    assert os.path.exists(restarted.merged_path)

    revised = tmp_path / "jia_v2.db"
    copy_with_overrides(branch_dbs[0], str(revised), delete_readings=(102,))
    restarted.replace_branch(BRANCH_A, "jia_v2.db", revised.read_bytes())

    conn = sqlite3.connect(restarted.merged_path)
    row = conn.execute(
        f"SELECT status FROM {MAP_TABLE} "
        f"WHERE branch=? AND table_name='readings' AND original_id=102",
        (BRANCH_A,),
    ).fetchone()
    assert row[0] == "withdrawn"
    conn.close()


def test_initialize_requires_both_branches(store, branch_dbs):
    with pytest.raises(MergeError):
        store.initialize(
            {BRANCH_A: ("jia.db", open(branch_dbs[0], "rb").read())}
        )
    assert not store.is_initialized()


def test_initialize_twice_rejected(store, branch_dbs):
    _init(store, branch_dbs)
    with pytest.raises(MergeError):
        _init(store, branch_dbs)


def test_replace_before_init_rejected(store, branch_dbs):
    with pytest.raises(MergeError):
        store.replace_branch(
            BRANCH_A, "jia.db", open(branch_dbs[0], "rb").read()
        )


def test_parallel_withdraw_readd_in_both_branches_keeps_distinct_ids(
    store, branch_dbs, tmp_path
):
    """两室同号记录各自撤下再补回，合库编号始终各自独立且稳定。"""
    _init(store, branch_dbs)
    conn = _conn(store)
    mids = {
        b: conn.execute(
            f"SELECT merged_id FROM {MAP_TABLE} "
            f"WHERE branch=? AND table_name='readings' AND original_id=102",
            (b,),
        ).fetchone()[0]
        for b in (BRANCH_A, BRANCH_B)
    }
    assert mids[BRANCH_A] != mids[BRANCH_B]
    conn.close()

    for branch, src in ((BRANCH_A, branch_dbs[0]), (BRANCH_B, branch_dbs[1])):
        withdrawn = tmp_path / f"{branch}_w.db"
        copy_with_overrides(src, str(withdrawn), delete_readings=(102,))
        store.replace_branch(branch, f"{branch}_w.db", withdrawn.read_bytes())

    conn = _conn(store)
    for branch, mid in mids.items():
        assert conn.execute(
            f"SELECT status FROM {MAP_TABLE} WHERE merged_id=? AND branch=? "
            f"AND table_name='readings' AND original_id=102",
            (mid, branch),
        ).fetchone()[0] == "withdrawn"
    conn.close()

    # 两室都补回
    store.replace_branch(BRANCH_A, "jia_back.db", open(branch_dbs[0], "rb").read())
    store.replace_branch(BRANCH_B, "yi_back.db", open(branch_dbs[1], "rb").read())
    conn = _conn(store)
    for branch, mid in mids.items():
        assert conn.execute(
            f"SELECT merged_id, status FROM {MAP_TABLE} "
            f"WHERE branch=? AND table_name='readings' AND original_id=102",
            (branch,),
        ).fetchone() == (mid, "present")
    conn.close()


def test_dangling_rejected_during_initialize(store, branch_dbs, tmp_path):
    """首次合库时任一库悬空即整体拒收，不产生合库文件。"""
    bad = tmp_path / "jia_bad.db"
    copy_with_overrides(
        branch_dbs[0],
        str(bad),
        extra_readings=(
            (301, 888, None, 999, "measurement", 200.0, 47.5, "悬空"),
        ),
    )
    with pytest.raises(MergeError) as exc:
        store.initialize(
            {
                BRANCH_A: ("jia_bad.db", bad.read_bytes()),
                BRANCH_B: ("yi.db", open(branch_dbs[1], "rb").read()),
            }
        )
    assert "888" in str(exc.value) and "999" in str(exc.value)
    assert not store.is_initialized()


def test_mapping_table_carries_branch_table_old_and_merged_ids(store, branch_dbs):
    """库内对照包含分室、表、原编号、合库编号四要素。"""
    _init(store, branch_dbs)
    rows = store.list_mapping()
    assert rows
    for r in rows:
        assert set(r) == {
            "branch", "table_name", "original_id", "merged_id", "status"
        }
        assert r["branch"] in (BRANCH_A, BRANCH_B)
        assert r["table_name"] in EXPORT_TABLES
    filtered = store.list_mapping(branch=BRANCH_A, table="tests")
    assert filtered and all(
        r["branch"] == BRANCH_A and r["table_name"] == "tests" for r in filtered
    )
