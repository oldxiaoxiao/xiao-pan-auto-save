"""数据库备份：快照生成 + 超出保留份数时裁剪最旧 + FR-11 一致性快照。

关键在「一致性」：WAL 模式下未 checkpoint 的提交只存在于 -wal 文件里，
直接复制主库会得到一份**能打开但少了数据**的库——比打不开更危险，因为它不说谎也不报错。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from backend.services import backup_service as bs
from backend.services.backup_service import backup_db


def make_db(tmp_path: Path) -> Path:
    """造一个真的 SQLite 库（不是假字节）：一致性快照只可能对真库生效。"""
    db = tmp_path / "xiao_pan.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE items (name TEXT)")
    conn.executemany("INSERT INTO items VALUES (?)", [(f"第{i}集",) for i in range(3)])
    conn.commit()
    conn.close()
    return db


def count_rows(db: Path) -> int:
    conn = sqlite3.connect(str(db))
    try:
        return int(conn.execute("SELECT COUNT(*) FROM items").fetchone()[0])
    finally:
        conn.close()


def test_backup_creates_snapshot(tmp_path):
    db = make_db(tmp_path)
    dest = backup_db(db, tmp_path / "backups")
    assert dest is not None and dest.exists()
    # FR-11 验收标准：备份能被完整打开，数据条数一致
    assert count_rows(dest) == 3


def test_backup_skips_missing_or_empty(tmp_path):
    assert backup_db(tmp_path / "nope.db", tmp_path / "backups") is None
    empty = tmp_path / "xiao_pan.db"
    empty.write_bytes(b"")
    assert backup_db(empty, tmp_path / "backups") is None


def test_backup_prunes_oldest(tmp_path):
    db = make_db(tmp_path)
    bdir = tmp_path / "backups"
    bdir.mkdir()
    # 预置几份明显更旧的快照（2020 年），字典序小于当前时间戳
    for i in range(4):
        (bdir / f"xiao_pan-2020010{i}-000000.db").write_bytes(b"old")
    dest = backup_db(db, bdir, keep=1)
    remaining = sorted(p.name for p in bdir.glob("xiao_pan-*.db"))
    assert remaining == [dest.name]  # 仅保留最新一份，旧的全被裁掉


def test_backup_captures_commits_still_in_wal(tmp_path):
    """核心回归点：数据还在 -wal 里没 checkpoint 时，备份也必须带上它。

    这是"下载高峰期备份"的真实形态。文件复制会漏掉这些提交，且副本照常能打开——
    等真要恢复时才发现少了一截。
    """
    db = tmp_path / "xiao_pan.db"
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")  # 关掉自动 checkpoint，让数据留在 -wal
    conn.execute("CREATE TABLE items (name TEXT)")
    conn.commit()
    for i in range(5):
        conn.execute("INSERT INTO items VALUES (?)", (f"第{i}集",))
        conn.commit()
    # 连接保持打开：此时主库文件里大概率还没有这些行
    dest = backup_db(db, tmp_path / "backups")
    assert dest is not None
    assert count_rows(dest) == 5
    conn.close()


def test_backup_falls_back_when_source_is_not_sqlite(tmp_path):
    """源文件压根不是数据库时，一致性快照无能为力：保底复制一份，但别假装它可信。"""
    db = tmp_path / "xiao_pan.db"
    db.write_bytes(b"not-a-database")
    dest = backup_db(db, tmp_path / "backups")
    assert dest is not None and dest.read_bytes() == b"not-a-database"


def test_backup_uses_backup_api_when_vacuum_unavailable(tmp_path, monkeypatch):
    """VACUUM INTO 不成（老版本 SQLite / 只读源）时，还有 online backup API 这条一致性退路。"""

    def broken(*_args, **_kwargs):
        raise sqlite3.OperationalError("VACUUM INTO 不可用")

    monkeypatch.setattr(bs, "_snapshot_vacuum", broken)
    db = make_db(tmp_path)
    dest = backup_db(db, tmp_path / "backups")
    assert dest is not None
    assert count_rows(dest) == 3  # 仍然是一致快照，不是文件复制


def test_snapshot_does_not_leave_partial_file(tmp_path, monkeypatch):
    """快照中途失败不能留下一个半截文件冒充备份。"""

    def broken(*_args, **_kwargs):
        raise sqlite3.OperationalError("boom")

    monkeypatch.setattr(bs, "_snapshot_vacuum", broken)
    monkeypatch.setattr(bs, "_snapshot_backup_api", broken)
    db = make_db(tmp_path)
    dest = backup_db(db, tmp_path / "backups")
    # 两条一致性路径都失败 → 退化为文件复制，内容仍是完整的主库（不是半截）
    assert dest is not None and dest.exists() and dest.stat().st_size > 0
