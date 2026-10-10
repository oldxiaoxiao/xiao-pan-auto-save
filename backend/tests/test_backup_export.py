"""FR-05 备份导出与恢复：脱敏导出、版本兼容、恢复前自动快照。"""

from __future__ import annotations

import json

import pytest
from sqlmodel import delete, select

from backend import __version__
from backend.database import session_scope
from backend.models import Account, DownloadRecord, Setting, Task
from backend.services import backup_service


@pytest.fixture(autouse=True)
def clean_db():
    with session_scope() as s:
        s.exec(delete(Task))
        s.exec(delete(Account))
        s.exec(delete(DownloadRecord))
    yield
    with session_scope() as s:
        s.exec(delete(Task))
        s.exec(delete(Account))
        s.exec(delete(DownloadRecord))


def _seed():
    with session_scope() as s:
        acc = Account(driver_key="quark", cookie="SECRET-COOKIE-VALUE", name="主号", enabled=True)
        s.add(acc)
        s.commit()
        s.refresh(acc)
        t = Task(taskname="剧集", shareurl="https://pan.quark.cn/s/1", savepath="/剧", account_id=acc.id)
        s.add(t)
        s.commit()
        s.refresh(t)
        s.add(DownloadRecord(task_id=t.id, filename="a.mp4", status="done", dest_path="/tmp/a.mp4"))
        s.commit()
        return acc, t


def test_safe_export_has_no_cookie():
    _seed()
    payload = backup_service.export_data("safe")

    assert payload["meta"]["kind"] == backup_service.BACKUP_KIND
    assert payload["meta"]["credentials_included"] is False
    assert payload["accounts"], "账号行要保留（恢复后才知道需要重新录入）"
    assert all(a["cookie"] == "" for a in payload["accounts"])
    assert "SECRET-COOKIE-VALUE" not in json.dumps(payload, ensure_ascii=False)


def test_full_export_keeps_cookie():
    _seed()
    payload = backup_service.export_data("full")
    assert payload["meta"]["credentials_included"] is True
    assert any(a["cookie"] == "SECRET-COOKIE-VALUE" for a in payload["accounts"])


def test_export_covers_all_four_kinds():
    _seed()
    with session_scope() as s:
        # merge：setting 以 key 为主键，其它用例可能已留下 crontab 行，直接 add 会撞 UNIQUE
        s.merge(Setting(key="crontab", value=json.dumps("0 9 * * *")))
    payload = backup_service.export_data("safe")
    assert payload["meta"]["counts"]["tasks"] >= 1
    assert payload["meta"]["counts"]["accounts"] >= 1
    assert payload["meta"]["counts"]["downloads"] >= 1
    assert "crontab" in payload["settings"]


def test_rejects_backup_from_newer_version(tmp_path):
    db = tmp_path / "xiao_pan.db"
    db.write_bytes(b"")
    payload = backup_service.export_data("safe")
    payload["meta"]["version"] = "99.0.0"

    result = backup_service.import_data(payload, db, tmp_path / "backups")
    assert result["ok"] is False
    assert "更高版本" in result["message"]


def test_rejects_foreign_file(tmp_path):
    db = tmp_path / "xiao_pan.db"
    db.write_bytes(b"")
    result = backup_service.import_data({"hello": "world"}, db, tmp_path / "backups")
    assert result["ok"] is False
    assert "不是本项目的备份" in result["message"]


def test_import_restores_and_snapshots_current(tmp_path):
    _seed()
    payload = backup_service.export_data("safe")

    db = tmp_path / "xiao_pan.db"
    db.write_bytes(b"current-db-content")  # 非空当前库 → 恢复前应先快照
    result = backup_service.import_data(payload, db, tmp_path / "backups")

    assert result["ok"] is True, result
    assert result["restored"]["tasks"] >= 1
    assert result["snapshot"], "恢复前必须给当前库留一份快照"
    with session_scope() as s:
        assert s.exec(select(Task)).first() is not None
        acc = s.exec(select(Account)).first()
        assert acc is not None and acc.cookie == "", "safe 备份恢复后 Cookie 应为空、待重新录入"


def test_import_full_backup_restores_cookie(tmp_path):
    _seed()
    payload = backup_service.export_data("full")

    db = tmp_path / "xiao_pan.db"
    db.write_bytes(b"x")
    result = backup_service.import_data(payload, db, tmp_path / "backups")
    assert result["ok"] is True
    with session_scope() as s:
        assert any(a.cookie == "SECRET-COOKIE-VALUE" for a in s.exec(select(Account)).all())


def test_current_version_backup_is_accepted(tmp_path):
    _seed()
    payload = backup_service.export_data("safe")
    assert payload["meta"]["version"] == __version__
    db = tmp_path / "xiao_pan.db"
    db.write_bytes(b"")
    assert backup_service.import_data(payload, db, tmp_path / "backups")["ok"] is True
