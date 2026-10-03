"""数据库备份：快照生成 + 超出保留份数时裁剪最旧。"""

from __future__ import annotations

from pathlib import Path

from backend.services.backup_service import backup_db


def make_db(tmp_path: Path) -> Path:
    db = tmp_path / "xiao_pan.db"
    db.write_bytes(b"sqlite-data")
    return db


def test_backup_creates_snapshot(tmp_path):
    db = make_db(tmp_path)
    bdir = tmp_path / "backups"
    dest = backup_db(db, bdir)
    assert dest is not None and dest.exists() and dest.read_bytes() == b"sqlite-data"


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
