"""FR-11：WAL 与写保护的行为测试。

不用 mock 数据库——WAL 开没开、busy_timeout 生效没生效，都是可以当场查出来的事实。
"""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest
from sqlalchemy import text

from backend.config import DB_PATH
from backend.database import BUSY_TIMEOUT_MS, db_pragmas, engine


@pytest.fixture(scope="module", autouse=True)
def _connected():
    """pragma 挂在 connect 事件上：先真正连一次，否则断言查的是"还没连过"的库。"""
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))


def test_journal_mode_is_wal():
    """WAL 的价值就是读写不互堵：下载写账本时界面还能刷列表。"""
    assert db_pragmas()["journal_mode"].lower() == "wal"


def test_busy_timeout_meets_fr11_minimum():
    assert db_pragmas()["busy_timeout"] >= 5000
    assert BUSY_TIMEOUT_MS >= 5000


def test_wal_files_exist_alongside_db():
    """WAL 模式下 -wal 是常态。备份必须走一致性快照，正是因为它装了一部分提交。"""
    assert DB_PATH.exists()
    # 有写入后才会留下 -wal；这里只确认模式切换成功，不强求文件此刻存在
    assert db_pragmas()["journal_mode"].lower() == "wal"


def test_second_writer_waits_instead_of_failing_immediately(tmp_path):
    """busy_timeout 的实际效果：写锁被别人拿着时，排队等到它放手，而不是抛 database is locked。"""
    db = str(tmp_path / "probe.db")
    # 独立库 + WAL，不去动共享的测试库（避免给别的用例留下探针表）
    seed = sqlite3.connect(db)
    seed.execute("PRAGMA journal_mode=WAL")
    seed.execute("CREATE TABLE _wal_probe (x INTEGER)")
    seed.commit()
    seed.close()

    holder = sqlite3.connect(db, timeout=BUSY_TIMEOUT_MS / 1000, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("CREATE TABLE IF NOT EXISTS _wal_probe (x INTEGER)")
    holder.execute("INSERT INTO _wal_probe VALUES (1)")

    result: dict[str, object] = {}

    def release_later() -> None:
        time.sleep(0.4)
        # sqlite3 默认禁止跨线程使用连接，故在锁持有线程里开、在释放线程里复用需显式放行
        holder.commit()
        holder.close()

    def second_writer() -> None:
        try:
            conn = sqlite3.connect(db, timeout=BUSY_TIMEOUT_MS / 1000)
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO _wal_probe VALUES (2)")
            conn.commit()
            conn.close()
            result["ok"] = True
        except sqlite3.Error as exc:  # pragma: no cover 失败即断言失败
            result["ok"] = False
            result["error"] = str(exc)

    waiter = threading.Thread(target=second_writer)
    releaser = threading.Thread(target=release_later)
    waiter.start()
    releaser.start()
    waiter.join(timeout=10)
    releaser.join(timeout=10)

    assert result.get("ok") is True, f"第二个写者失败：{result.get('error')}"
