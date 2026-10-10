"""SQLite 数据库：engine、建表、会话。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlmodel import Session, SQLModel, create_engine

from .config import DB_PATH

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
    echo=False,
)


def init_db() -> None:
    from . import models  # noqa: F401  确保表注册

    SQLModel.metadata.create_all(engine)
    _auto_add_columns()
    _normalize_unchecked_accounts()
    _encrypt_plaintext_cookies()


def _encrypt_plaintext_cookies() -> None:
    """FR-08：把存量明文 Cookie 加密。加密前后取值一致，升级不中断已有任务。"""
    try:
        from .services.credential_store import migrate_plaintext_cookies

        moved = migrate_plaintext_cookies()
        if moved:
            from .core.logstream import hub

            hub.make_logger("startup")("info", f"已将 {moved} 个账号的 Cookie 转为加密存储")
    except Exception:  # noqa: BLE001 迁移失败不影响启动
        pass


def _normalize_unchecked_accounts() -> None:
    """「从没检查过」的账号一律归位为健康。

    补列默认值（老版本补成 0）或新建账号都可能留下 check_ok=0 但没有检查记录的行，
    直接读这个字段会得出"失效"的错误结论。判据以 last_check_at 为准，这里把脏值修平。
    """
    from sqlalchemy import text

    try:
        with engine.begin() as conn:
            conn.execute(text("UPDATE account SET check_ok = 1 WHERE check_ok = 0 AND last_check_at IS NULL"))
    except Exception:  # noqa: BLE001 归一失败不影响启动
        pass


def _auto_add_columns() -> None:
    """轻量迁移：给已存在的表补齐模型新增列（SQLite ALTER ADD COLUMN）。"""
    from sqlalchemy import Integer, inspect, text

    inspector = inspect(engine)
    for table in SQLModel.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        existing = {c["name"] for c in inspector.get_columns(table.name)}
        with engine.begin() as conn:
            for col in table.columns:
                if col.name in existing:
                    continue
                coltype = col.type.compile(engine.dialect)
                if col.nullable:
                    default = ""
                elif isinstance(col.type, Integer) or "BOOL" in coltype.upper():
                    # 取模型写的默认值，别一律补 0：check_ok 这类"默认健康"的布尔列
                    # 补成 0 会让所有存量账号一夜之间被标成"需更新"。
                    py_default = getattr(col.default, "arg", None) if col.default is not None else None
                    default = f" NOT NULL DEFAULT {1 if py_default else 0}"
                else:
                    py_default = getattr(col.default, "arg", None) if col.default is not None else None
                    default = f" NOT NULL DEFAULT '{py_default if isinstance(py_default, str) else ''}'"
                conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {col.name} {coltype}{default}"))


@contextmanager
def session_scope() -> Iterator[Session]:
    # expire_on_commit=False：允许在会话关闭后继续读取已加载的实例属性
    with Session(engine, expire_on_commit=False) as session:
        yield session
        session.commit()
