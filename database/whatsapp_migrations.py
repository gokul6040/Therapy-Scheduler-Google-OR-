"""Idempotent timezone migration scoped only to WhatsApp button-click records."""

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine


def migrate_whatsapp_button_times(engine: Engine) -> None:
    """Preserve old UTC instants and add database-generated Asia/Kolkata times."""
    if engine.dialect.name not in {"postgresql", "sqlite"}:
        return
    with engine.begin() as connection:
        if engine.dialect.name == "postgresql":
            # Serialize migrations if two replicas start simultaneously.
            connection.execute(text("SELECT pg_advisory_xact_lock(87652514)"))
        inspector = inspect(connection)
        if not inspector.has_table("whatsapp_button_clicks"):
            return
        columns = {column["name"]: column for column in inspector.get_columns("whatsapp_button_clicks")}
        if engine.dialect.name == "postgresql":
            for name in ("clicked_at", "recorded_at"):
                if not columns[name]["type"].timezone:
                    connection.execute(text(
                        f"ALTER TABLE whatsapp_button_clicks ALTER COLUMN {name} "
                        f"TYPE TIMESTAMP WITH TIME ZONE USING {name} AT TIME ZONE 'UTC'"
                    ))
        if "time_zone" not in columns:
            connection.execute(text(
                "ALTER TABLE whatsapp_button_clicks ADD COLUMN time_zone "
                "VARCHAR(64) NOT NULL DEFAULT 'Asia/Kolkata'"
            ))
        for source in ("clicked_at", "recorded_at"):
            target = f"{source}_ist"
            if target in columns:
                continue
            if engine.dialect.name == "postgresql":
                expression = f"{source} AT TIME ZONE 'Asia/Kolkata'"
                persistence = "STORED"
            else:
                expression = f"datetime({source}, '+5 hours', '+30 minutes') || substr({source}, 20)"
                persistence = "VIRTUAL"
            connection.execute(text(
                f"ALTER TABLE whatsapp_button_clicks ADD COLUMN {target} "
                f"TIMESTAMP GENERATED ALWAYS AS ({expression}) {persistence} NOT NULL"
            ))
