from sqlalchemy import inspect

from clawbox.common.db import Base, engine, init_db


def test_database_initialization_is_idempotent() -> None:
    Base.metadata.drop_all(engine)
    init_db()
    init_db()
    assert set(Base.metadata.tables).issubset(set(inspect(engine).get_table_names()))
