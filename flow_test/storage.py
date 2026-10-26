"""Route shared ORM models to isolated test tables without changing the models."""

from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine, make_url

from config import DATABASE_URL
from database.db import Base
from database.models import (
    AdminBookingDecision, AdminSlotReservation, Appointment, AuditLog, Department,
    Location, ParentAvailabilityResponse, Patient, PatientAbsence,
    PatientAvailability, Therapist, TherapistAbsence, TherapistAvailability,
    TreatmentRequirement,
)
from flow_test.models import Base as FlowBase
from flow_test.models import FlowContactWindow, FlowInbox, FlowJob, FlowOffer, FlowSetting


ALLOWED_SCHEMAS = ("flow_test", "flow_test_preview")
TEST_MODELS = (
    Department, Location, Therapist, TherapistAvailability, TherapistAbsence,
    Patient, PatientAvailability, PatientAbsence, TreatmentRequirement,
    Appointment, AuditLog, AdminBookingDecision, AdminSlotReservation,
    ParentAvailabilityResponse, FlowSetting, FlowOffer, FlowJob, FlowInbox,
    FlowContactWindow,
)
TEST_TABLES = tuple(model.__table__ for model in TEST_MODELS)


def validate_test_target(database_url: str, schema: str | None = None) -> None:
    """Shared PostgreSQL is allowed only with an explicit test namespace."""
    url = make_url(database_url)
    backend = url.get_backend_name()
    if schema is not None and schema not in ALLOWED_SCHEMAS:
        raise ValueError("Use only the flow_test or flow_test_preview schema")
    if schema is not None and backend != "postgresql":
        raise ValueError("Named test schemas require PostgreSQL")
    if schema is None:
        if url == make_url(DATABASE_URL):
            raise ValueError("The test must not use the main application database without an isolated schema")
        if backend == "postgresql" and not (url.database or "").endswith("_flow_test"):
            raise ValueError("Specify a test schema or use a dedicated database ending in _flow_test")
    if backend not in {"postgresql", "sqlite"}:
        raise ValueError("Use SQLite or PostgreSQL test storage")


def create_test_engine(database_url: str, schema: str | None = None) -> Engine:
    """Schema mapping is per engine; main ORM tables and sessions stay unchanged."""
    validate_test_target(database_url, schema)
    backend = make_url(database_url).get_backend_name()
    options = {"hide_parameters": True}
    if backend == "sqlite":
        options["connect_args"] = {"check_same_thread": False}
    else:
        options["pool_pre_ping"] = True
        options["connect_args"] = {"connect_timeout": 15}
        if schema:
            # No public fallback. This also isolates unqualified PostgreSQL enum
            # creation/checks without modifying shared SQLAlchemy type objects.
            options["connect_args"]["options"] = f"-csearch_path={schema},pg_catalog"
            options["execution_options"] = {"schema_translate_map": {None: schema}}
    return create_engine(database_url, **options)


def initialize_test_tables(engine: Engine, schema: str | None = None) -> None:
    """Create only flow dependencies in the selected namespace, never public."""
    inspector = inspect(engine)
    if schema and not inspector.has_schema(schema):
        raise ValueError("Provision the test schema before starting the test app")
    existing = set(inspector.get_table_names(schema=schema))
    if existing and "flow_test_settings" not in existing:
        raise ValueError("Refusing to initialize test storage without the flow-test marker")
    unexpected = existing - {table.name for table in TEST_TABLES}
    if unexpected:
        raise ValueError("Test namespace contains unrelated tables; initialization stopped")
    # A transaction ensures a failed PostgreSQL DDL batch leaves no partial set.
    with engine.begin() as connection:
        Base.metadata.create_all(connection, tables=[
            table for table in TEST_TABLES if table.metadata is Base.metadata
        ])
        FlowBase.metadata.create_all(connection)
