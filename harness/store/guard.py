"""append-only 的「DDL 防线」：拆掉触发器这件事本身要变难，而且一旦发生必须被检出。

**为什么需要它**（独立验证 2026-09-19 · P2-5 实测）：``events`` 的只读历史原先只由
SQLite 触发器保证，而触发器是 schema 对象——任何拿到连接的人都能一句
``DROP TRIGGER events_no_update`` 把它拆掉，随后的 ``UPDATE`` 就畅通无阻
（实测：drop 之后 ``UPDATE events SET payload_json=...`` 被接受）。
v4 的第三条触发器堵住的是 ``INSERT OR REPLACE`` 这条**普通 DML** 路径，
DDL 路径一直敞着。本模块补上三层：

* **连接层（防）**：``install_connection_guard`` 在 ``SqliteStore`` 自己的连接上装两道
  机制——``SQLITE_DBCONFIG_DEFENSIVE``（让 ``PRAGMA writable_schema=ON`` 静默失效、
  ``sqlite_master`` 不可写）与 ``set_authorizer`` 回调（拒绝 ``DROP TRIGGER`` /
  ``DROP TABLE`` / ``DROP VIEW`` / ``DROP INDEX`` / ``ALTER TABLE``、对 ``sqlite_master``
  的 UPDATE / DELETE、``PRAGMA writable_schema``）。**authorizer 是必须的**（缺它则拒绝该连接，
  fail-closed）；DEFENSIVE 需要 Python 3.12+，缺它时降级为"只有 authorizer"并如实记录，
  因为 ``writable_schema`` 那条路 authorizer 自己就挡住了（3.11 上实测）。
* **写前核查（检测）**：``assert_guard_intact`` 把 ``sqlite_master`` 里的 ``events`` 表结构
  与三条触发器定义，逐字对回 ``DDL_TABLES`` / ``TRIGGER_STATEMENTS``；
  ``SqliteStore.append_many`` 在 ``BEGIN IMMEDIATE`` 之后、写入之前跑它——
  **别人拆掉防线之后，本进程的下一次追加会 fail-closed**，而不是继续往一个
  不再只读的库里写。``SqliteStore.setup`` 同样核查，并且**默认不顺手修**：
  版本已是最新却防线不全，那不是"待跑的迁移"能解释的差异（要修必须写
  ``setup(allow_repair=True)``，让"我知道这个库被动过"这件事留在调用点）。
* **离线核查**：``verify_append_only_guard``（连 ``-wal``/``-shm`` 快照后检查）与
  ``harness.audit_chain`` 结果里的 ``guard`` 字段，供事后审计。

**残余边界（不要读过头）**：连接层防线只对**本进程这一条连接**生效。拿到数据库文件的人
可以另开一个没有防线、也没有触发器的连接——他能改文件，也能在改完之后把触发器文本与
``PRAGMA schema_version`` 一起凑成"看起来没被动过"的样子。链没有密钥，所以这不是
密码学意义上的防篡改：**防线的作用是把"静默改写"的窗口从"任意时刻的一行 SQL"压缩成
"能写这个文件的人刻意伪造 schema"，并让后者在离线审计里留下痕迹。**
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .schema import DDL_TABLES, TRIGGER_STATEMENTS
from .snapshot import snapshot_db

#: 只读历史所在的表。防线的一切核查都围绕它。
GUARDED_TABLE = "events"

_IF_NOT_EXISTS = re.compile(r"\bif\s+not\s+exists\b", re.IGNORECASE)
_WHITESPACE = re.compile(r"\s+")
_TRIGGER_NAME = re.compile(
    r"CREATE\s+TRIGGER\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)
# schema 表在 SQLite 3.33+ 有两个拼法，authorizer 的 arg1 用哪个都可能
_SCHEMA_TABLES = frozenset({"sqlite_master", "sqlite_schema"})


class AppendOnlyGuardError(RuntimeError):
    """append-only 防线不完整／已被拆掉（与 ``StoreError`` 平级，都属于"拒绝继续"）。"""


# --------------------------------------------------------------------- 文本归一化


def _strip_comments(sql: str) -> str:
    """去掉 ``--`` 行注释——只在字符串字面量之外动手，免得被引号里的 ``--`` 骗到。"""
    out: list[str] = []
    index = 0
    quote: str | None = None
    while index < len(sql):
        char = sql[index]
        if quote is not None:
            out.append(char)
            if char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    # 双写转义（SQL 里 '' 就是一个引号），原样保留
                    out.append(sql[index + 1])
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if char in ("'", '"'):
            quote = char
            out.append(char)
            index += 1
            continue
        if sql.startswith("--", index):
            while index < len(sql) and sql[index] != "\n":
                index += 1
            continue
        out.append(char)
        index += 1
    return "".join(out)


def normalize_sql(sql: str) -> str:
    """把 DDL 文本归一到"语义相同就相等"的规范形式。

    归一化的对象是 **SQLite 自己回读出来的** ``sqlite_master.sql`` 与仓库里的
    规范文本，两者的差异只有三处（实测）：SQLite 存库时丢掉 ``IF NOT EXISTS``、
    去掉外层空白、去掉结尾的分号。逐字比较因此需要一个共同的规范形式。

    归一化只在**双方**都做，所以它不会把"不同"说成"相同"：去掉注释与折叠空白
    都不改变 SQL 的记号序列（换行折叠成空格不会把两个记号粘成一个）。
    """
    text = _strip_comments(sql)
    text = _IF_NOT_EXISTS.sub(" ", text)
    text = _WHITESPACE.sub(" ", text)
    return text.strip().rstrip(";").strip()


@lru_cache(maxsize=1)
def canonical_triggers() -> dict[str, str]:
    """规范触发器：名字 → 归一化后的定义文本（从 ``TRIGGER_STATEMENTS`` 现取）。"""
    out: dict[str, str] = {}
    for statement in TRIGGER_STATEMENTS:
        match = _TRIGGER_NAME.search(statement)
        if match is None:
            raise AppendOnlyGuardError(
                f"TRIGGER_STATEMENTS 里有解析不出名字的语句：{statement.strip()[:60]!r}"
            )
        out[match.group(1)] = normalize_sql(statement)
    return out


# ------------------------------------------------------------------ 表结构指纹


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def table_fingerprint(conn: sqlite3.Connection, table: str = GUARDED_TABLE) -> tuple:
    """``events`` 的**结构**指纹：列（名/类型/NOT NULL/主键/默认值）+ 表级 UNIQUE 约束。

    为什么不逐字比对 ``CREATE TABLE`` 文本：``ALTER TABLE ... ADD COLUMN``（v2→v3 迁移
    补链列就是这么加的）会被 SQLite 把新列定义写进原文本的结尾，于是**合法迁移过的库**
    文本必然与新建库不同。结构指纹既能认出这种合法差异，又能抓住真正的不变量
    （主键 ``event_id``、``UNIQUE (branch_id, seq)``）——第三层 INSERT 守卫的冲突
    判定正是建立在这两条约束上，所以它们必须是结构事实，而不是文本巧合。
    """
    columns = tuple(
        (str(row[1]), (row[2] or "").upper(), int(row[3]), int(row[5]), row[4])
        for row in conn.execute(f"PRAGMA table_info({_quote(table)})")
    )
    uniques: list[tuple[str, ...]] = []
    for row in conn.execute(f"PRAGMA index_list({_quote(table)})"):
        if int(row[2]) and str(row[3]) == "u":  # 只认表级 UNIQUE，不认 CREATE INDEX
            uniques.append(
                tuple(
                    str(item[2])
                    for item in conn.execute(f"PRAGMA index_info({_quote(str(row[1]))})")
                )
            )
    return (columns, tuple(sorted(uniques)))


@lru_cache(maxsize=1)
def reference_fingerprint() -> tuple:
    """规范结构：**现建一个内存库**跑 ``DDL_TABLES`` 再读回来，不手抄一份期望值。"""
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(DDL_TABLES)
        return table_fingerprint(conn)
    finally:
        conn.close()


# ------------------------------------------------------------------------ 核查


def guard_report(conn: sqlite3.Connection) -> dict[str, Any]:
    """核查 append-only 防线的当前状态。只读，不改动任何东西。

    返回 ``{"ok", "table_ok", "expected_triggers", "missing_triggers",
    "altered_triggers", "unexpected_triggers", "problems"}``。
    """
    problems: list[str] = []
    expected = canonical_triggers()
    try:
        rows = conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master").fetchall()
    except sqlite3.DatabaseError as exc:
        return {
            "ok": False,
            "table_ok": False,
            "expected_triggers": sorted(expected),
            "missing_triggers": sorted(expected),
            "altered_triggers": [],
            "unexpected_triggers": [],
            "problems": [f"读 sqlite_master 失败：{type(exc).__name__}: {exc}"],
        }

    tables = {str(row[1]) for row in rows if str(row[0]) == "table"}
    triggers = {
        str(row[1]): (str(row[2]), row[3] or "") for row in rows if str(row[0]) == "trigger"
    }

    table_ok = GUARDED_TABLE in tables
    if not table_ok:
        problems.append(f"表 {GUARDED_TABLE!r} 不存在")
    else:
        try:
            observed = table_fingerprint(conn)
        except sqlite3.DatabaseError as exc:
            observed = None
            problems.append(f"读 {GUARDED_TABLE!r} 结构失败：{type(exc).__name__}: {exc}")
        if observed is not None and observed != reference_fingerprint():
            table_ok = False
            problems.append(
                f"表 {GUARDED_TABLE!r} 的结构与规范 DDL 不一致"
                "（主键 / NOT NULL / 表级 UNIQUE 之一被改过）"
            )

    missing: list[str] = []
    altered: list[str] = []
    for name, canonical in expected.items():
        found = triggers.get(name)
        if found is None:
            missing.append(name)
            problems.append(f"触发器 {name!r} 不存在（历史已不再是物理只读）")
            continue
        owner, sql = found
        if owner != GUARDED_TABLE:
            altered.append(name)
            problems.append(f"触发器 {name!r} 挂在表 {owner!r} 上，不再是 {GUARDED_TABLE!r}")
        elif normalize_sql(sql) != canonical:
            altered.append(name)
            problems.append(f"触发器 {name!r} 的定义与规范文本不一致（内容被改过）")

    unexpected = sorted(
        name
        for name, (owner, _sql) in triggers.items()
        if owner == GUARDED_TABLE and name not in expected
    )
    if unexpected:
        problems.append(
            f"表 {GUARDED_TABLE!r} 上出现了未登记的触发器：{', '.join(unexpected)}"
            "（谁能加上它，谁就能改写入语义）"
        )

    return {
        "ok": not problems,
        "table_ok": table_ok,
        "expected_triggers": sorted(expected),
        "missing_triggers": missing,
        "altered_triggers": altered,
        "unexpected_triggers": unexpected,
        "problems": problems,
    }


def assert_guard_intact(conn: sqlite3.Connection) -> dict[str, Any]:
    """核查不通过就抛 :class:`AppendOnlyGuardError`；通过则返回报告。

    调用点都在**写入之前**（``setup()`` 与 ``append_many()`` 的事务内），
    所以防线被拆掉之后本进程是 fail-closed 的：不写，而不是往一个不再只读的
    库里继续追加。
    """
    report = guard_report(conn)
    if not report["ok"]:
        raise AppendOnlyGuardError(
            "append-only 防线不完整，拒绝继续写："
            + "；".join(report["problems"])
            + "。要重建触发器必须显式写 `SqliteStore(path).setup(allow_repair=True)`"
            "（默认的 `setup()` 在版本已是最新却防线不全时会拒绝——顺手修好会把"
            "「这个库曾经不只读」变回静默）；若来源不明，按\"已被改写\"处理："
            "用 `harness.audit_chain --db` 校验链，并与可信来源的链头比对。"
        )
    return report


# ------------------------------------------------------------- 连接层：防（拒绝）


def _enable_defensive(conn: sqlite3.Connection) -> bool:
    """打开 ``SQLITE_DBCONFIG_DEFENSIVE``；这一层需要 Python 3.12+，缺了不算失败。"""
    config = getattr(sqlite3, "SQLITE_DBCONFIG_DEFENSIVE", None)
    setconfig = getattr(conn, "setconfig", None)
    if config is None or setconfig is None:
        return False
    try:
        setconfig(config, True)
    except (sqlite3.Error, ValueError, TypeError):
        return False
    return True


_DENIED_ACTIONS = frozenset(
    action
    for action in (
        getattr(sqlite3, "SQLITE_DROP_TRIGGER", None),
        getattr(sqlite3, "SQLITE_DROP_TABLE", None),
        getattr(sqlite3, "SQLITE_DROP_VIEW", None),
        getattr(sqlite3, "SQLITE_DROP_INDEX", None),
        getattr(sqlite3, "SQLITE_ALTER_TABLE", None),
    )
    if action is not None
)
_WRITE_ACTIONS = frozenset(
    action
    for action in (
        # 只拒 UPDATE / DELETE，**不拒 INSERT**：``CREATE TABLE IF NOT EXISTS`` 即使表已存在，
        # SQLite 也会以 ``SQLITE_INSERT / sqlite_master`` 先请求授权（实测），拒掉它等于把
        # 幂等的 DDL 一起拒了（``setup()`` 重跑就崩）。而往 sqlite_master 里插一行这条路
        # 本来就走不通：没有 ``writable_schema`` 时 SQLite 自己报
        # "table sqlite_master may not be modified"，开了 ``writable_schema`` 又过不了
        # 下面那条 PRAGMA 拒绝（加上 DEFENSIVE 这一层，它是静默失效）。
        getattr(sqlite3, "SQLITE_UPDATE", None),
        getattr(sqlite3, "SQLITE_DELETE", None),
    )
    if action is not None
)


def _authorizer_callback(
    action: int, arg1: str | None, _arg2: str | None, _db: str | None, _trigger: str | None
) -> int:
    """拒绝"拆防线"的那几类动作；其余一律放行。

    ``CREATE TRIGGER`` **必须放行**：``setup()`` 的 ``executescript(DDL)`` 与 v3/v4 迁移
    都要（重）建触发器；多出来的触发器由 :func:`guard_report` 报出来，它们改不了
    三条守卫的语义（守卫是 ABORT，不会被别的触发器抵消）。
    """
    if action in _DENIED_ACTIONS:
        return sqlite3.SQLITE_DENY
    name = arg1.lower() if isinstance(arg1, str) else ""
    if action in _WRITE_ACTIONS and name in _SCHEMA_TABLES:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_PRAGMA and name == "writable_schema":
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


@dataclass(frozen=True)
class GuardStatus:
    """连接层防线的实际状态（如实记录哪一层真的装上了）。"""

    defensive: bool
    authorizer: bool
    schema_version: int

    def describe(self) -> str:
        defensive = "on" if self.defensive else "off（需 Python 3.12+；writable_schema 仍被拒）"
        return (
            "append-only 连接层防线："
            f"authorizer={'on' if self.authorizer else 'OFF'}，defensive={defensive}"
        )


def install_connection_guard(conn: sqlite3.Connection) -> GuardStatus:
    """给连接装上 DDL 防线；authorizer 不可用则拒收该连接（fail-closed）。

    **必须在 DDL 与迁移之后调用**：v3 迁移要临时 ``DROP TRIGGER`` 再重建，
    而这里装上 authorizer 之后 ``DROP TRIGGER`` 会被拒。
    """
    if not hasattr(conn, "set_authorizer"):
        raise AppendOnlyGuardError(
            "这个 sqlite3 连接没有 set_authorizer：DDL 防线无法安装。"
            "append-only 不能降级成\"只有触发器\"，因此拒绝继续（fail-closed）。"
        )
    conn.set_authorizer(_authorizer_callback)
    defensive = _enable_defensive(conn)
    cookie = int(conn.execute("PRAGMA schema_version").fetchone()[0])
    return GuardStatus(defensive=defensive, authorizer=True, schema_version=cookie)


# ------------------------------------------------------------------ 离线核查


def verify_append_only_guard(db_path: str | Path) -> dict[str, Any]:
    """离线核查一个库文件（**连 ``-wal``/``-shm`` 一起快照**，纪律的唯一实现在 snapshot.py）。

    与 ``harness.audit_chain`` 的区别：那个回答"链有没有断"，这个只回答
    "append-only 防线还在不在"。两者都要问：触发器被拆掉之后，链可能仍然是完整的
    （改历史的人若同时重算了哈希），而"历史不可被静默改写"这个承诺已经没了。
    """
    path = Path(db_path)
    if not path.exists():
        return {"ok": False, "db": str(path), "error": f"库不存在：{path}", "guard": None}
    conn = sqlite3.connect(snapshot_db(path))
    try:
        report = guard_report(conn)
    except sqlite3.DatabaseError as exc:
        return {
            "ok": False,
            "db": str(path),
            "error": f"库无法读取：{type(exc).__name__}: {exc}",
            "guard": None,
        }
    finally:
        conn.close()
    return {"ok": bool(report["ok"]), "db": str(path), "guard": report}
