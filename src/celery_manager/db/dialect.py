"""Dialect-specific SQL kept out of application control paths."""
from sqlalchemy import DateTime
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.functions import FunctionElement


class utcnow(FunctionElement):
    type = DateTime(timezone=True)
    inherit_cache = True


@compiles(utcnow)
def _utcnow(element, compiler, **kwargs):
    return "CURRENT_TIMESTAMP"


@compiles(utcnow, "mssql")
def _mssql_utcnow(element, compiler, **kwargs):
    return "SYSUTCDATETIME()"


@compiles(utcnow, "mysql")
@compiles(utcnow, "mariadb")
def _mysql_utcnow(element, compiler, **kwargs):
    return "(UTC_TIMESTAMP())"


def schema_of(connection):
    return (connection.get_execution_options().get("schema_translate_map") or {}).get(None)
