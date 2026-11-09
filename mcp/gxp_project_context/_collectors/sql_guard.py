from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from sqlglot import exp, parse
from sqlglot.errors import ParseError


class SqlRejected(ValueError):
    pass


IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
PARAMETER = re.compile(r"%\([A-Za-z_][A-Za-z0-9_]*\)s|%s")
DANGEROUS_TEXT = re.compile(
    r"\b(INTO\s+(?:OUTFILE|DUMPFILE)|FOR\s+UPDATE|LOCK\s+IN\s+SHARE\s+MODE|"
    r"EXPLAIN\s+ANALYZE|PROCEDURE\s+ANALYSE)\b|"
    r"\b(SLEEP|BENCHMARK|LOAD_FILE)\s*\(|:=|@@|@[A-Za-z_]",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class GuardResult:
    statement_type: str
    normalized_sql: str
    sql_hash: str


def validate_identifier(value: str, kind: str = "identifier") -> str:
    if not IDENTIFIER.fullmatch(value or ""):
        raise SqlRejected(f"Invalid {kind}: {value!r}")
    return value


def _expression_classes(*names: str) -> tuple[type[exp.Expression], ...]:
    result: list[type[exp.Expression]] = []
    for name in names:
        candidate = getattr(exp, name, None)
        if isinstance(candidate, type):
            result.append(candidate)
    return tuple(result)


DENIED_TYPES = _expression_classes(
    "Insert",
    "Update",
    "Delete",
    "Create",
    "Drop",
    "Alter",
    "Merge",
    "Copy",
    "Command",
    "Transaction",
    "Commit",
    "Rollback",
    "Set",
    "Grant",
    "Revoke",
    "Use",
    "Lock",
    "Unlock",
)


def validate_readonly_sql(sql: str, database: str) -> GuardResult:
    if not sql or not sql.strip():
        raise SqlRejected("SQL cannot be empty")
    if DANGEROUS_TEXT.search(sql):
        raise SqlRejected("SQL contains a forbidden read-side effect or expensive operation")

    parse_sql = PARAMETER.sub("0", sql)
    try:
        expressions = [item for item in parse(parse_sql, read="mysql") if item is not None]
    except ParseError as exc:
        raise SqlRejected(f"SQL cannot be parsed as MySQL: {exc}") from exc
    if len(expressions) != 1:
        raise SqlRejected("Exactly one SQL statement is allowed")

    expression = expressions[0]
    statement_type = type(expression).__name__
    allowed_root = isinstance(expression, exp.Query) or statement_type in {
        "Show",
        "Describe",
        "Desc",
        "Explain",
    }
    if not allowed_root:
        raise SqlRejected(f"Statement type {statement_type} is not allowed")

    if statement_type == "Show":
        show_type = str(expression.args.get("this") or "").upper()
        allowed_show = {
            "TABLES",
            "TABLE STATUS",
            "COLUMNS",
            "FIELDS",
            "INDEX",
            "INDEXES",
            "KEYS",
            "CREATE TABLE",
        }
        if show_type not in allowed_show:
            raise SqlRejected(f"SHOW {show_type or 'statement'} is not allowed")
        show_database = expression.args.get("db")
        if show_database:
            schema = str(getattr(show_database, "name", show_database) or "").lower()
            if schema and schema != database.lower():
                raise SqlRejected(
                    f"SHOW may only access database {database!r}; found {schema!r}"
                )

    for node in expression.walk():
        if DENIED_TYPES and isinstance(node, DENIED_TYPES):
            raise SqlRejected(f"SQL node {type(node).__name__} is not allowed")

    allowed_database = database.lower()
    for table in expression.find_all(exp.Table):
        catalog = str(table.catalog or "").lower()
        schema = str(table.db or "").lower()
        if catalog:
            raise SqlRejected("Cross-catalog queries are not allowed")
        if schema and schema != allowed_database:
            raise SqlRejected(
                f"Raw SQL may only access database {database!r}; found {schema!r}"
            )

    normalized = expression.sql(dialect="mysql", pretty=False)
    return GuardResult(
        statement_type=statement_type,
        normalized_sql=normalized,
        sql_hash=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
    )
