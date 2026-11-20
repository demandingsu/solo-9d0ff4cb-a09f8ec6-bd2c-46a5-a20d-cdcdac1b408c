"""生成甲/乙分室示例子库。

两个分室库沿用项目示例表结构（与 app.sampledb 同一套 DDL），且刻意使用
相同的原编号（groups/calibrations/tests/readings 各表 id 一致），用于验证
合库时同号记录在各分室内独立编号、互不串扰。记录内容以分室标签区分。
"""
from __future__ import annotations

import sqlite3

from .sampledb import DDL


def _rows(branch: str) -> dict[str, list[tuple]]:
    return {
        "groups": [
            (1, f"{branch}分室-墙体试件A", "240mm 混凝土砌块墙"),
            (2, f"{branch}分室-公共设施", "背景噪声与对照测量"),
        ],
        "calibrations": [
            (1, f"声级计 NL-52（{branch}）", "2026-01-15", f"CERT-{branch}-001"),
        ],
        "tests": [
            (1, 1, 1, f"{branch}-计权隔声-第1次", "GB/T 19889.3", "2026-03-02"),
            (2, 2, 1, f"{branch}-背景噪声对照", "GB/T 19889.3", "2026-03-01"),
        ],
        # (id, test_id, calibration_id, control_reading_id, kind, freq, level, note)
        "readings": [
            (101, 1, 1, None, "measurement", 100.0, 52.1, None),
            # 对照引用本分室读数 101，校准引用可空
            (102, 1, None, 101, "measurement", 125.0, 51.0, None),
            (201, 2, 1, 101, "control", 100.0, 30.2, None),
        ],
    }


def create_branch_db(path: str, branch: str) -> str:
    """生成一个分室的示例子库。branch 通常为 '甲' 或 '乙'。"""
    rows = _rows(branch)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(DDL)
        conn.executemany("INSERT INTO groups VALUES (?,?,?)", rows["groups"])
        conn.executemany(
            "INSERT INTO calibrations VALUES (?,?,?,?)", rows["calibrations"]
        )
        conn.executemany(
            "INSERT INTO tests VALUES (?,?,?,?,?,?)", rows["tests"]
        )
        conn.executemany(
            "INSERT INTO readings VALUES (?,?,?,?,?,?,?,?)", rows["readings"]
        )
        conn.commit()
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"分室示例库外键校验失败：{violations}")
    finally:
        conn.close()
    return path


def copy_with_overrides(
    src_path: str,
    out_path: str,
    *,
    delete_readings: tuple[int, ...] = (),
    extra_readings: tuple[tuple, ...] = (),
) -> str:
    """基于一个分室库复制出修订版：可撤下读数或追加读数（用于换库测试）。"""
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    out = sqlite3.connect(out_path)
    try:
        ddl = ";\n".join(
            sql.rstrip(";")
            for (sql,) in src.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE sql IS NOT NULL AND type IN ('table','index') "
                "ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END, "
                "CASE tbl_name "
                "WHEN 'groups' THEN 0 WHEN 'calibrations' THEN 1 "
                "WHEN 'tests' THEN 2 WHEN 'readings' THEN 3 ELSE 4 END"
            )
            if sql
        )
        out.executescript(ddl)
        for table in ("groups", "calibrations", "tests"):
            cur = src.execute(f"SELECT * FROM {table}")
            out.executemany(
                f"INSERT INTO {table} VALUES "
                f"({','.join('?' * len(cur.description))})",
                cur.fetchall(),
            )
        if delete_readings:
            placeholders = ",".join("?" * len(delete_readings))
            cur = src.execute(
                f"SELECT * FROM readings WHERE id NOT IN ({placeholders})",
                delete_readings,
            )
        else:
            cur = src.execute("SELECT * FROM readings")
        out.executemany(
            f"INSERT INTO readings VALUES "
            f"({','.join('?' * len(cur.description))})",
            cur.fetchall(),
        )
        if extra_readings:
            out.executemany(
                "INSERT INTO readings VALUES (?,?,?,?,?,?,?,?)", extra_readings
            )
        out.commit()
    finally:
        src.close()
        out.close()
    return out_path
