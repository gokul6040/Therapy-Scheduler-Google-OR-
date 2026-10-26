from datetime import datetime, timedelta

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from database.models import WhatsAppButtonClick
from database.whatsapp_migrations import migrate_whatsapp_button_times
from database.whatsapp_time import as_kolkata


def test_legacy_utc_clicks_get_ist_columns_once_without_changing_instant():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE whatsapp_button_clicks (event_key VARCHAR(64) PRIMARY KEY, "
            "clicked_at TIMESTAMP NOT NULL, recorded_at TIMESTAMP NOT NULL)"
        ))
        connection.execute(text(
            "INSERT INTO whatsapp_button_clicks VALUES ('old', '2026-10-14 09:04:47.000000', "
            "'2026-10-14 09:04:48.891773')"
        ))
    for _ in range(2):
        migrate_whatsapp_button_times(engine)
    with engine.connect() as connection:
        row = connection.execute(text("SELECT * FROM whatsapp_button_clicks")).mappings().one()
        assert row["clicked_at"] == "2026-10-14 09:04:47.000000"
        assert row["clicked_at_ist"] == "2026-10-14 14:34:47.000000"
        assert row["recorded_at_ist"] == "2026-10-14 14:34:48.891773"
        assert row["time_zone"] == "Asia/Kolkata"
        assert inspect(connection).get_columns("whatsapp_button_clicks")[-1]["computed"]
    engine.dispose()


def test_postgresql_schema_has_timezone_aware_instants_and_stored_ist_columns():
    ddl = str(CreateTable(WhatsAppButtonClick.__table__).compile(dialect=postgresql.dialect()))
    assert "TIMESTAMP WITH TIME ZONE" in ddl
    assert "clicked_at AT TIME ZONE 'Asia/Kolkata'" in ddl
    assert "recorded_at AT TIME ZONE 'Asia/Kolkata'" in ddl
    assert ddl.count("STORED") == 2


def test_legacy_naive_input_is_utc_and_aware_input_is_not_double_shifted():
    naive = datetime(2026, 10, 14, 10)
    aware = as_kolkata(naive)
    assert aware.hour == 15
    assert aware.minute == 30
    assert aware.utcoffset() == timedelta(hours=5, minutes=30)
    assert as_kolkata(aware) == aware
