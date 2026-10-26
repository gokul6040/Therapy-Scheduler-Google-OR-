"""Test shared-database namespace isolation without a production connection."""

from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CreateTable

from database.db import Base
from database.models import Appointment, Patient
from flow_test.provision_postgres import read_settings, save_settings
from flow_test.service import FlowTest
from flow_test.storage import (
    TEST_TABLES, create_test_engine, initialize_test_tables, validate_test_target,
)


def test_shared_postgres_requires_an_explicit_test_namespace():
    url = "postgresql+psycopg://test:fake@localhost/postgres"
    with pytest.raises(ValueError, match="test schema"):
        validate_test_target(url)
    validate_test_target(url, "flow_test")
    validate_test_target(url, "flow_test_preview")
    for schema in ("public", "pg_catalog", "flow_test;DROP SCHEMA public", ""):
        with pytest.raises(ValueError, match="schema"):
            validate_test_target(url, schema)
    with pytest.raises(ValueError, match="PostgreSQL"):
        validate_test_target("sqlite://", "flow_test")


def test_postgres_engine_qualifies_models_and_has_no_public_search_path(monkeypatch):
    captured = {}
    sentinel = object()

    def fake_engine(url, **kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr("flow_test.storage.create_engine", fake_engine)
    assert create_test_engine("postgresql+psycopg://test:fake@localhost/postgres", "flow_test") is sentinel
    assert captured["execution_options"] == {"schema_translate_map": {None: "flow_test"}}
    assert captured["connect_args"]["options"] == "-csearch_path=flow_test,pg_catalog"
    assert captured["hide_parameters"] is True
    ddl = str(CreateTable(Appointment.__table__).compile(
        dialect=postgresql.dialect(), schema_translate_map={None: "flow_test"},
        render_schema_translate=True,
    ))
    assert "CREATE TABLE flow_test.appointments" in ddl
    assert "REFERENCES flow_test.patients" in ddl
    assert Appointment.__table__.schema is None


def test_full_flow_uses_test_namespace_and_preserves_existing_tables(monkeypatch, legacy_flow_scenario):
    # SQLite's attached DB mimics a PostgreSQL schema to exercise the exact same
    # shared ORM mapping and repair transactions without external credentials.
    original = create_engine("sqlite://", poolclass=StaticPool,
                             connect_args={"check_same_thread": False})
    Base.metadata.create_all(original, tables=TEST_TABLES)
    with original.begin() as conn:
        conn.execute(Patient.__table__.insert().values(
            patient_code="EXISTING-UNCHANGED", name="Existing fictional control",
            active=True, school_going=False,
        ))
        conn.exec_driver_sql("ATTACH DATABASE ':memory:' AS flow_test")
    mapped = original.execution_options(schema_translate_map={None: "flow_test"})
    monkeypatch.setattr("flow_test.service.create_test_engine", lambda *args: mapped)
    flow = FlowTest("sqlite://", database_schema="flow_test", scenario=legacy_flow_scenario)
    try:
        target = date(2026, 10, 19)
        assert flow.generate(target)["added"] == 4
        assert flow.publish(target) == 3
        flow.drain()
        token = next(o["token"] for o in flow.state(target)["offers"] if o["patient"] == "TEST-B")
        flow.simulate(token, "DAY")
        flow.drain()
        assert any(a["patient"] == "TEST-D" and a["status"] == "PROPOSED"
                   for a in flow.state(target)["appointments"])
        with original.connect() as conn:
            assert conn.exec_driver_sql("SELECT patient_code FROM main.patients").scalars().all() == ["EXISTING-UNCHANGED"]
            assert conn.exec_driver_sql("SELECT count(*) FROM main.appointments").scalar_one() == 0
            assert conn.exec_driver_sql("SELECT count(*) FROM main.flow_test_jobs").scalar_one() == 0
        with sessionmaker(bind=mapped)() as session:
            assert set(session.scalars(select(Patient.patient_code))) == {"TEST-A", "TEST-B", "TEST-C", "TEST-D"}
        # Expansion uses the same schema mapping, never the public/main control.
        saved = flow.state(target)["appointments"]
        expanded = FlowTest("sqlite://", database_schema="flow_test")
        assert expanded.state(target)["appointments"] == saved
        with sessionmaker(bind=mapped)() as session:
            assert len(list(session.scalars(select(Patient)))) == 8
        expanded.reset_test_data("RESET preview")
        assert expanded.state(target)["appointments"] == []
        with original.connect() as conn:
            assert conn.exec_driver_sql("SELECT patient_code FROM main.patients").scalars().all() == ["EXISTING-UNCHANGED"]
            assert conn.exec_driver_sql("SELECT count(*) FROM main.appointments").scalar_one() == 0
        assert all(table.schema is None for table in TEST_TABLES)
    finally:
        original.dispose()


def test_initializer_refuses_missing_schema_and_unrelated_tables():
    engine = create_engine("sqlite://")
    try:
        with pytest.raises(ValueError, match="Provision"):
            initialize_test_tables(engine, "flow_test")
        with engine.begin() as conn:
            conn.exec_driver_sql("CREATE TABLE existing_data (id INTEGER)")
        with pytest.raises(ValueError, match="marker"):
            initialize_test_tables(engine)
    finally:
        engine.dispose()


def test_test_namespace_has_only_the_needed_tables_and_complete_foreign_keys():
    names = {table.name for table in TEST_TABLES}
    assert len(names) == 19
    assert "appointments" in names and "flow_test_contact_windows" in names
    assert not any("zoho" in name or name.endswith("_source") for name in names)
    for table in TEST_TABLES:
        for foreign_key in table.foreign_keys:
            assert foreign_key.column.table.name in names


def test_saving_restricted_db_settings_preserves_whatsapp_secrets(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# keep comment\nWHATSAPP_ACCESS_TOKEN=fake-secret\nFLOW_TEST_MESSAGE_MODE=session\n", encoding="utf-8")
    save_settings(path, {"FLOW_TEST_DATABASE_URL": "postgresql://test:fake@localhost/postgres",
                         "FLOW_TEST_DATABASE_SCHEMA": "flow_test"})
    settings = read_settings(path)
    assert settings["WHATSAPP_ACCESS_TOKEN"] == "fake-secret"
    assert settings["FLOW_TEST_MESSAGE_MODE"] == "session"
    assert settings["FLOW_TEST_DATABASE_SCHEMA"] == "flow_test"
    assert path.read_text(encoding="utf-8").startswith("# keep comment\n")
