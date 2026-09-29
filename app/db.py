"""数据库层：SQLAlchemy 2.0 引擎与会话。

本地开发用 SQLite，生产用 MySQL —— 同一套 ORM 代码，靠 DATABASE_URL 切换。
"""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

_connect_args = {"check_same_thread": False} if settings.DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(
    settings.DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    connect_args=_connect_args,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：每个请求一个会话，用完必关。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """建表（幂等）。生产环境应改用 Alembic 迁移。"""
    from app import models  # noqa: F401  触发模型注册

    Base.metadata.create_all(bind=engine)
