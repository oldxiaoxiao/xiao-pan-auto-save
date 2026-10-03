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
                    default = " NOT NULL DEFAULT 0"
                else:
                    default = " NOT NULL DEFAULT ''"
                conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {col.name} {coltype}{default}"))


@contextmanager
def session_scope() -> Iterator[Session]:
    # expire_on_commit=False：允许在会话关闭后继续读取已加载的实例属性
    with Session(engine, expire_on_commit=False) as session:
        yield session
        session.commit()
