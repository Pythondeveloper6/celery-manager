"""Security identities stay exact even under case-insensitive DB collations."""
import pytest
from sqlalchemy import Column, MetaData, String, Table, create_engine, select
from sqlalchemy.dialects import mssql, mysql, postgresql, sqlite
from sqlalchemy.schema import CreateTable

from celery_manager.db.identity import exact_identity, identity_type


def test_binary_filter_survives_case_insensitive_text_prefilter():
    engine = create_engine("sqlite://")
    table = Table("identities", MetaData(), Column("value", String(255, collation="NOCASE")))
    table.metadata.create_all(engine)
    values = ["tenant", "TENANT", "tenant ", "ténant", "te\u0301nant", "شركة", None]
    with engine.begin() as connection:
        connection.execute(table.insert(), [{"value": value} for value in values])
        assert connection.scalars(select(table.c.value).where(table.c.value == "tenant")).all() == ["tenant", "TENANT"]
        for value in values:
            assert connection.scalars(select(table.c.value).where(exact_identity(table.c.value, value))).all() == [value]


@pytest.mark.parametrize("dialect,fragments", [
    (sqlite.dialect(), (" AS BLOB)",)),
    (postgresql.dialect(), ("convert_to(", "'UTF8'")),
    (mysql.dialect(), ("USING utf8mb4", " AS BINARY)")),
    (mssql.dialect(), ("AS NVARCHAR(max)", "AS VARBINARY(max)")),
])
def test_exact_predicate_compiles_an_index_prefilter_and_binary_comparison(dialect, fragments):
    table = Table("identities", MetaData(), Column("value", identity_type()))
    sql = str(select(table.c.value).where(exact_identity(table.c.value, "Ténant ")).compile(dialect=dialect))
    assert "identities.value = " in sql
    assert " AND " in sql
    assert all(fragment in sql for fragment in fragments)
    assert "Ténant" not in sql  # identifiers are bound, never interpolated


def test_unicode_identity_storage_is_explicit_on_mysql_and_sql_server():
    table = Table("identities", MetaData(), Column("value", identity_type()))
    assert "NVARCHAR(255)" in str(CreateTable(table).compile(dialect=mssql.dialect()))
    assert "CHARACTER SET utf8mb4" in str(CreateTable(table).compile(dialect=mysql.dialect()))


def test_portable_text_limit_counts_supplementary_characters_twice():
    from celery_manager.db.identity import portable_text_fits
    assert portable_text_fits("😀" * 127 + "x", 255)
    assert not portable_text_fits("😀" * 128, 255)
    assert portable_text_fits("x" * 255, 255)
    assert not portable_text_fits("x" * 256, 255)
    assert not portable_text_fits("\ud800", 255)


@pytest.mark.parametrize("name", ["tenant_id", "correlation_id", "operation_key"])
def test_context_identity_bounds_are_checked_before_database_access(name):
    from celery_manager.run_context import normalize_context
    valid = "😀" * 127 + "x"
    assert normalize_context({name: valid})[name] == valid
    with pytest.raises(ValueError, match=name + ".*UTF-16"):
        normalize_context({name: "😀" * 128})


def test_environment_uses_same_portable_storage_bound():
    from celery_manager import CeleryManagerConfig
    assert CeleryManagerConfig(celery_app=None, environment_name="😀" * 50).environment_name == "😀" * 50
    with pytest.raises(ValueError, match="environment_name.*UTF-16"):
        CeleryManagerConfig(celery_app=None, environment_name="😀" * 51)


def test_new_annotation_text_columns_preserve_unicode_on_server_dialects():
    from celery_manager.db.models import OperationAnnotation
    sql_server = str(CreateTable(OperationAnnotation.__table__).compile(dialect=mssql.dialect()))
    mysql_sql = str(CreateTable(OperationAnnotation.__table__).compile(dialect=mysql.dialect()))
    assert "label NVARCHAR(255)" in sql_server and "revision NVARCHAR(255)" in sql_server
    assert "note NTEXT" in sql_server or "note NVARCHAR(max)" in sql_server
    assert "label VARCHAR(255) CHARACTER SET utf8mb4" in mysql_sql
    assert "revision VARCHAR(255) CHARACTER SET utf8mb4" in mysql_sql
    assert "note TEXT CHARACTER SET utf8mb4" in mysql_sql
