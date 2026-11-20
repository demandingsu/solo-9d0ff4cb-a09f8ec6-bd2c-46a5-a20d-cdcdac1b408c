"""双分室子库合库核心逻辑。

复核员需要在一个合库中查看甲、乙两个分室的隔声试验数据。两个分室各自提交
采用项目示例表结构（groups / calibrations / tests / readings 四表）的 SQLite
子库，本模块负责：

1. 首次合库：同时接收甲、乙两份子库，校验结构与引用完整性后合成一个合库；
2. 单独换库：之后任一分室可单独上传新版子库替换，合库整体重建；
3. 编号对照：合库内维护 merge_id_map，记录 分室 / 表 / 原编号 / 合库编号 /
   记录状态（present 在库、withdrawn 已撤下）；
4. 编号稳定性：同一分室同一表的同一原编号，一旦分配过合库编号就永久占用——
   记录撤下后从数据表移除但对照保留；日后补回沿用原合库编号；该编号绝不转给
   其他记录；
5. 引用归属：所有外键（分组、试验归属、对照读数、校准引用）只在各自分室内
   解析并重写到合库编号，分室之间不发生引用串扰；
6. 失败保护：换库先落临时文件并完整校验，再原子替换；任一步失败时旧合库与
   编号对照原样保留，仍可下载。

合库文件持久化在 DATA_DIR/merge 下，进程重启后状态不丢。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime

from .core import (
    CALIBRATIONS,
    EXPORT_TABLES,
    GROUPS,
    READINGS,
    TESTS,
    validate_schema,
)

BRANCH_A = "jia"
BRANCH_B = "yi"
BRANCHES = (BRANCH_A, BRANCH_B)
BRANCH_LABELS = {BRANCH_A: "甲", BRANCH_B: "乙"}

MERGE_DIRNAME = "merge"
MERGED_NAME = "merged.db"
BRANCH_FILES = {BRANCH_A: "jia.db", BRANCH_B: "yi.db"}

# 合库内的编号对照表与分室元数据表
MAP_TABLE = "merge_id_map"
META_TABLE = "merge_branches"

# 各数据表的外键列 → 被引用表（仅项目结构约定内的关键列）
FK_COLUMNS: dict[str, dict[str, str]] = {
    TESTS: {"group_id": GROUPS, "calibration_id": CALIBRATIONS},
    READINGS: {
        "test_id": TESTS,
        "calibration_id": CALIBRATIONS,
        "control_reading_id": READINGS,
    },
}

MAP_DDL = f"""
CREATE TABLE {MAP_TABLE} (
    branch TEXT NOT NULL,
    table_name TEXT NOT NULL,
    original_id INTEGER NOT NULL,
    merged_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'present',
    PRIMARY KEY (branch, table_name, original_id),
    UNIQUE (table_name, merged_id)
)
"""

META_DDL = f"""
CREATE TABLE {META_TABLE} (
    branch TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    source_name TEXT,
    updated_at TEXT NOT NULL
)
"""


@dataclass
class Dangling:
    """一条悬空引用：某表某行的某外键列指向了不存在的目标。"""

    table: str
    row_id: int
    column: str
    target_table: str
    target_id: int

    @property
    def message(self) -> str:
        return (
            f"表 {self.table} 行 id={self.row_id} 的列 {self.column} 悬空："
            f"{self.target_table} 中不存在 id={self.target_id} 的记录"
        )


@dataclass
class BranchValidation:
    schema_errors: list[str] = field(default_factory=list)
    dangling: list[Dangling] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.schema_errors and not self.dangling


class MergeError(Exception):
    """合库操作被拒收（结构不符、悬空引用、状态非法等）。"""

    def __init__(
        self,
        message: str,
        *,
        branch: str | None = None,
        validation: BranchValidation | None = None,
    ):
        super().__init__(message)
        self.branch = branch
        self.validation = validation


def validate_branch(path: str) -> BranchValidation:
    """校验单个分室子库：结构约定 + 外键悬空检查。"""
    result = BranchValidation()
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.DatabaseError:
        result.schema_errors.append("文件不是有效的 SQLite 数据库")
        return result
    try:
        try:
            result.schema_errors = validate_schema(conn)
        except sqlite3.DatabaseError:
            result.schema_errors.append("文件不是有效的 SQLite 数据库")
            return result
        if result.schema_errors:
            return result
        for table, fks in FK_COLUMNS.items():
            for col, target in fks.items():
                cur = conn.execute(
                    f"SELECT t.id, t.{col} FROM {table} t "
                    f"WHERE t.{col} IS NOT NULL "
                    f"AND t.{col} NOT IN (SELECT id FROM {target})"
                )
                for row_id, target_id in cur.fetchall():
                    result.dangling.append(
                        Dangling(table, row_id, col, target, target_id)
                    )
        result.dangling.sort(
            key=lambda d: (d.table, d.row_id, d.column, d.target_id)
        )
    finally:
        conn.close()
    return result


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


@dataclass
class BranchMeta:
    branch: str
    label: str
    source_name: str | None
    updated_at: str | None


@dataclass
class MergeStatus:
    initialized: bool
    metas: dict[str, BranchMeta]
    # branch -> table -> 在库记录数（不含 withdrawn）
    counts: dict[str, dict[str, int]]


class MergeStore:
    """合库工作区：管理分室文件、合库文件与编号对照，线程安全。"""

    def __init__(self, data_dir: str):
        self.root = os.path.join(data_dir, MERGE_DIRNAME)
        self.merged_path = os.path.join(self.root, MERGED_NAME)
        self._lock = threading.RLock()

    # ---- 路径与状态 ----

    def branch_path(self, branch: str) -> str:
        return os.path.join(self.root, BRANCH_FILES[branch])

    def is_initialized(self) -> bool:
        return os.path.exists(self.merged_path)

    def status(self) -> MergeStatus:
        with self._lock:
            if not self.is_initialized():
                return MergeStatus(False, {}, {})
            conn = sqlite3.connect(
                f"file:{self.merged_path}?mode=ro", uri=True
            )
            try:
                metas: dict[str, BranchMeta] = {}
                for branch, label, name, ts in conn.execute(
                    f"SELECT branch, label, source_name, updated_at "
                    f"FROM {META_TABLE}"
                ):
                    metas[branch] = BranchMeta(branch, label, name, ts)
                counts: dict[str, dict[str, int]] = {
                    b: {t: 0 for t in EXPORT_TABLES} for b in BRANCHES
                }
                for branch, table, n in conn.execute(
                    f"SELECT branch, table_name, COUNT(*) FROM {MAP_TABLE} "
                    f"WHERE status='present' GROUP BY branch, table_name"
                ):
                    counts.setdefault(branch, {})[table] = n
            finally:
                conn.close()
            return MergeStatus(True, metas, counts)

    def list_mapping(
        self, branch: str | None = None, table: str | None = None
    ) -> list[dict]:
        """读取编号对照（含已撤下记录），供网页展示。"""
        if not self.is_initialized():
            return []
        where, params = [], []
        if branch:
            where.append("branch = ?")
            params.append(branch)
        if table:
            where.append("table_name = ?")
            params.append(table)
        sql = (
            f"SELECT branch, table_name, original_id, merged_id, status "
            f"FROM {MAP_TABLE}"
        )
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY table_name, branch, original_id"
        conn = sqlite3.connect(f"file:{self.merged_path}?mode=ro", uri=True)
        try:
            return [
                dict(
                    zip(
                        ("branch", "table_name", "original_id", "merged_id", "status"),
                        row,
                    )
                )
                for row in conn.execute(sql, params)
            ]
        finally:
            conn.close()

    # ---- 写入入口 ----

    def initialize(self, files: dict[str, tuple[str, bytes]]) -> None:
        """首次合库：必须同时提供甲、乙两份子库。"""
        with self._lock:
            if self.is_initialized():
                raise MergeError("合库已初始化，请使用各分室的单独换库功能")
            missing = [b for b in BRANCHES if b not in files]
            if missing:
                labels = "、".join(BRANCH_LABELS[b] for b in missing)
                raise MergeError(f"首次合库需同时上传甲、乙两份子库，缺少：{labels}")
            self._ingest(files)

    def replace_branch(self, branch: str, filename: str, data: bytes) -> None:
        """单独替换一个分室的子库。"""
        with self._lock:
            if branch not in BRANCH_FILES:
                raise MergeError(f"未知分室：{branch}")
            if not self.is_initialized():
                raise MergeError("合库尚未初始化，请先完成甲、乙两库的首次上传")
            self._ingest({branch: (filename, data)})

    # ---- 内部实现 ----

    def _ingest(self, files: dict[str, tuple[str, bytes]]) -> None:
        """校验通过后原子落盘并重建合库；失败时不改动任何现有文件。"""
        os.makedirs(self.root, exist_ok=True)
        staged: dict[str, str] = {}
        try:
            # 1. 写临时文件并逐份校验（结构 + 悬空引用）
            for branch, (filename, data) in files.items():
                if not filename:
                    raise MergeError(
                        f"{BRANCH_LABELS[branch]}分室未选择文件", branch=branch
                    )
                tmp = os.path.join(self.root, f".{branch}.staging.db")
                staged[branch] = tmp
                with open(tmp, "wb") as f:
                    f.write(data)
                validation = validate_branch(tmp)
                if not validation.ok:
                    detail = "；".join(
                        [*validation.schema_errors]
                        + [d.message for d in validation.dangling]
                    )
                    raise MergeError(
                        f"{BRANCH_LABELS[branch]}分室子库校验未通过，已拒收：{detail}",
                        branch=branch,
                        validation=validation,
                    )

            # 2. 列结构兼容性检查
            self._check_columns(staged)

            # 3. 确定各分室本次重建的读取来源（新库读临时文件，其余读现文件）
            sources: dict[str, str] = {}
            for branch in BRANCHES:
                sources[branch] = staged.get(
                    branch, self.branch_path(branch)
                )
                if not os.path.exists(sources[branch]):
                    raise MergeError(
                        f"{BRANCH_LABELS[branch]}分室子库缺失，无法重建合库"
                    )

            # 4. 重建到临时合库文件（旧合库全程不动）
            tmp_merged = self.merged_path + ".tmp"
            if os.path.exists(tmp_merged):
                os.remove(tmp_merged)
            names = {b: name for b, (name, _d) in files.items()}
            self._rebuild(sources, names, tmp_merged)

            # 5. 全部成功后原子替换
            for branch, tmp in staged.items():
                os.replace(tmp, self.branch_path(branch))
            os.replace(tmp_merged, self.merged_path)
        finally:
            for tmp in staged.values():
                if os.path.exists(tmp):
                    os.remove(tmp)
            tmp_merged = self.merged_path + ".tmp"
            if os.path.exists(tmp_merged):
                os.remove(tmp_merged)

    def _check_columns(self, staged: dict[str, str]) -> None:
        """首次合库比对两库列结构；换库比对新库与现合库的列结构。"""
        if not self.is_initialized():
            if len(staged) < 2:
                return
            ref_path = staged[BRANCH_A]
            other_path = staged[BRANCH_B]
            ref_conn = sqlite3.connect(f"file:{ref_path}?mode=ro", uri=True)
            other_conn = sqlite3.connect(f"file:{other_path}?mode=ro", uri=True)
            try:
                for table in EXPORT_TABLES:
                    ref = _table_columns(ref_conn, table)
                    other = _table_columns(other_conn, table)
                    if ref != other:
                        raise MergeError(
                            f"甲、乙两库表 {table} 列结构不一致，无法合库："
                            f"甲={ref}；乙={other}"
                        )
            finally:
                ref_conn.close()
                other_conn.close()
        else:
            merged = sqlite3.connect(
                f"file:{self.merged_path}?mode=ro", uri=True
            )
            try:
                for branch, tmp in staged.items():
                    new_conn = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
                    try:
                        for table in EXPORT_TABLES:
                            old_cols = _table_columns(merged, table)
                            new_cols = _table_columns(new_conn, table)
                            if old_cols != new_cols:
                                raise MergeError(
                                    f"{BRANCH_LABELS[branch]}分室新库表 {table} "
                                    f"列结构与现合库不一致，已拒收："
                                    f"现合库={old_cols}；新库={new_cols}",
                                    branch=branch,
                                )
                    finally:
                        new_conn.close()
            finally:
                merged.close()

    def _load_old_mapping(
        self,
    ) -> dict[tuple[str, str, int], tuple[int, str]]:
        """读取旧合库的编号对照；首次合库时为空。"""
        if not self.is_initialized():
            return {}
        conn = sqlite3.connect(f"file:{self.merged_path}?mode=ro", uri=True)
        try:
            return {
                (branch, table, oid): (mid, status)
                for branch, table, oid, mid, status in conn.execute(
                    f"SELECT branch, table_name, original_id, merged_id, status "
                    f"FROM {MAP_TABLE}"
                )
            }
        finally:
            conn.close()

    def _rebuild(
        self,
        sources: dict[str, str],
        new_names: dict[str, str],
        out_path: str,
    ) -> None:
        """按各分室现文件重建合库，编号对照沿用旧合库并保证不重用编号。"""
        old_mapping = self._load_old_mapping()
        conns = {
            branch: sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            for branch, path in sources.items()
        }
        # DDL 以甲库（或任一可用分室库）为准，两库列结构已校验一致
        ddl_branch = next(b for b in BRANCHES if b in conns)
        out = sqlite3.connect(out_path)
        try:
            out.execute("PRAGMA foreign_keys = OFF")
            ddl: dict[str, str] = {}
            extras: list[str] = []
            for typ, _name, tbl, sql in conns[ddl_branch].execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE sql IS NOT NULL"
            ):
                if typ == "table" and tbl in EXPORT_TABLES:
                    ddl[tbl] = sql
                elif typ in ("index", "trigger") and tbl in EXPORT_TABLES:
                    extras.append(sql)
            for table in EXPORT_TABLES:
                out.execute(ddl[table])

            # (branch, table, original_id) -> (merged_id, status)
            new_mapping: dict[tuple[str, str, int], tuple[int, str]] = {}

            for table in EXPORT_TABLES:
                # 该表已被占用的合库编号：含历史撤下记录，编号永不复用
                used_ids = {
                    mid
                    for (b, t, _oid), (mid, _st) in old_mapping.items()
                    if t == table
                }
                next_id = 1

                def alloc(branch: str, oid: int) -> int:
                    nonlocal next_id
                    key = (branch, table, oid)
                    if key in old_mapping:
                        mid = old_mapping[key][0]  # 补回/仍在：沿用原合库编号
                    else:
                        while next_id in used_ids:
                            next_id += 1
                        mid = next_id
                        used_ids.add(mid)
                        next_id += 1
                    new_mapping[key] = (mid, "present")
                    return mid

                # 第一遍：为每个分室的每行分配合库编号（分室顺序固定，结果确定）
                id_by_branch: dict[str, dict[int, int]] = {}
                for branch in BRANCHES:
                    if branch not in conns:
                        continue
                    oids = [
                        r[0]
                        for r in conns[branch].execute(
                            f"SELECT id FROM {table} ORDER BY id"
                        )
                    ]
                    id_by_branch[branch] = {oid: alloc(branch, oid) for oid in oids}

                # 第二遍：按原列读取、重写外键到合库编号后插入
                for branch in BRANCHES:
                    if branch not in conns:
                        continue
                    src = conns[branch]
                    cur = src.execute(f"SELECT * FROM {table}")
                    cols = [d[0] for d in cur.description]
                    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
                    fks = FK_COLUMNS.get(table, {})
                    insert_rows = []
                    for row in rows:
                        out_row = dict(row)
                        out_row["id"] = id_by_branch[branch][row["id"]]
                        for col, target_table in fks.items():
                            if row.get(col) is None:
                                continue
                            target_key = (branch, target_table, row[col])
                            # 悬空引用已在入库前拒收；这里仅作防御性检查
                            if target_key not in new_mapping:
                                raise MergeError(
                                    f"{BRANCH_LABELS[branch]}分室表 {table} "
                                    f"行 id={row['id']} 的列 {col} 悬空："
                                    f"{target_table} 中不存在 id={row[col]} 的记录"
                                )
                            out_row[col] = new_mapping[target_key][0]
                        insert_rows.append(
                            [out_row[c] for c in cols]
                        )
                    if insert_rows:
                        out.executemany(
                            f"INSERT INTO {table} ({','.join(cols)}) "
                            f"VALUES ({','.join('?' * len(cols))})",
                            insert_rows,
                        )

            # 历史撤下记录：移出数据表，但编号对照原样保留
            for key, (mid, _status) in old_mapping.items():
                if key not in new_mapping:
                    new_mapping[key] = (mid, "withdrawn")

            for sql in extras:
                out.execute(sql)
            out.executescript(MAP_DDL)
            out.executescript(META_DDL)
            out.executemany(
                f"INSERT INTO {MAP_TABLE} "
                f"(branch, table_name, original_id, merged_id, status) "
                f"VALUES (?,?,?,?,?)",
                [
                    (branch, table, oid, mid, status)
                    for (branch, table, oid), (mid, status) in sorted(
                        new_mapping.items()
                    )
                ],
            )

            now = datetime.now().isoformat(timespec="seconds")
            for branch in BRANCHES:
                if branch not in conns:
                    continue
                if branch in new_names:
                    name, ts = new_names[branch], now
                else:
                    name, ts = self._old_meta(branch)
                out.execute(
                    f"INSERT INTO {META_TABLE} "
                    f"(branch, label, source_name, updated_at) VALUES (?,?,?,?)",
                    (branch, BRANCH_LABELS[branch], name, ts),
                )

            out.commit()
            violations = out.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise MergeError(f"合库外键校验失败：{violations}")
        finally:
            out.close()
            for c in conns.values():
                c.close()

    def _old_meta(self, branch: str) -> tuple[str | None, str | None]:
        """从旧合库读取某分室的文件名与更新时间（换库时未替换的分室沿用）。"""
        if not self.is_initialized():
            return None, None
        conn = sqlite3.connect(f"file:{self.merged_path}?mode=ro", uri=True)
        try:
            row = conn.execute(
                f"SELECT source_name, updated_at FROM {META_TABLE} "
                f"WHERE branch = ?",
                (branch,),
            ).fetchone()
            return row if row else (None, None)
        finally:
            conn.close()

    def remove_all(self) -> None:
        """测试辅助：清空整个合库工作区。"""
        with self._lock:
            shutil.rmtree(self.root, ignore_errors=True)
