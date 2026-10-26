"""Timezone-aware instants and persisted Indian-time projections for click records."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import DateTime
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.functions import FunctionElement
from sqlalchemy.types import TypeDecorator


WHATSAPP_TIME_ZONE = "Asia/Kolkata"
KOLKATA = ZoneInfo(WHATSAPP_TIME_ZONE)


def kolkata_now() -> datetime:
    return datetime.now(KOLKATA)


def as_kolkata(value: datetime) -> datetime:
    """Interpret legacy naive inputs as UTC, then return an aware IST datetime."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(KOLKATA)


class KolkataTimestamp(TypeDecorator):
    """Use timestamptz in PostgreSQL; preserve the same instant in SQLite tests."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        value = as_kolkata(value)
        if dialect.name == "sqlite":
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    def process_result_value(self, value, dialect):
        return as_kolkata(value) if value is not None else None


class KolkataWallTime(FunctionElement):
    """Compile a generated IST wall-clock column for each supported database."""

    type = DateTime()
    inherit_cache = True


@compiles(KolkataWallTime, "postgresql")
def _postgres_kolkata_time(element, compiler, **kw):
    column = compiler.process(list(element.clauses)[0], **kw)
    return f"({column} AT TIME ZONE 'Asia/Kolkata')"


@compiles(KolkataWallTime, "sqlite")
def _sqlite_kolkata_time(element, compiler, **kw):
    column = compiler.process(list(element.clauses)[0], **kw)
    # Keep the microsecond suffix that SQLite's datetime() otherwise discards.
    return f"(datetime({column}, '+5 hours', '+30 minutes') || substr({column}, 20))"
