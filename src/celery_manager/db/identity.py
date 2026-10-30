"""Exact opaque identities, independent of a database's default text collation."""
from sqlalchemy import LargeBinary, Unicode, and_, bindparam
from sqlalchemy.dialects.mysql import VARCHAR
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.functions import FunctionElement


def portable_text_fits(value, limit):
    """Portable VARCHAR/NVARCHAR bound, counting supplementary Unicode twice."""
    if not isinstance(value, str) or len(value) > limit:
        return False
    try:
        return len(value.encode("utf-16-le")) <= limit * 2
    except UnicodeEncodeError:
        return False


def identity_type(length=255):
    """Unicode storage even when a MySQL database defaults to a legacy charset."""
    return Unicode(length).with_variant(VARCHAR(length, charset="utf8mb4"), "mysql")


class _IdentityBytes(FunctionElement):
    type = LargeBinary()
    inherit_cache = True


@compiles(_IdentityBytes)
def _default_bytes(element, compiler, **kw):
    return f"CAST({compiler.process(element.clauses, **kw)} AS BLOB)"


@compiles(_IdentityBytes, "postgresql")
def _postgres_bytes(element, compiler, **kw):
    return f"convert_to(CAST({compiler.process(element.clauses, **kw)} AS TEXT), 'UTF8')"


@compiles(_IdentityBytes, "mysql")
def _mysql_bytes(element, compiler, **kw):
    return f"CAST(CONVERT({compiler.process(element.clauses, **kw)} USING utf8mb4) AS BINARY)"


@compiles(_IdentityBytes, "mssql")
def _mssql_bytes(element, compiler, **kw):
    return f"CAST(CAST({compiler.process(element.clauses, **kw)} AS NVARCHAR(max)) AS VARBINARY(max))"


def exact_identity(column, value):
    """Keep an indexed prefilter, then require byte-exact case/accent/space identity.

    Text equality alone is unsafe for tenant isolation under common MySQL and
    SQL Server collations. Binary comparison also distinguishes trailing spaces,
    which SQL Server text equality ignores even under a binary collation.
    """
    if value is None:
        return column.is_(None)
    parameter = bindparam(None, value, type_=Unicode(255))
    return and_(column == parameter, _IdentityBytes(column) == _IdentityBytes(parameter))
