from sqlalchemy import func, select

from database.db import init_db
from database.models import AuditLog, Patient, PlanDetailSource
from importers.workbook_importer import import_sanitized_workbook, load_authoritative_source_tables


def initialize_source_database(session):
    """Load the sanitized current-plan workbook and operating availability policy."""
    init_db()
    source_rows = {"plan_detail": 0, "therapist_master": 0, "specialty_master": 0}
    if session.scalar(select(PlanDetailSource.source_row_number).limit(1)) is None:
        source_rows = load_authoritative_source_tables(session)
    if session.scalar(select(func.count(Patient.id))):
        session.commit()
        return {
            "initialized": False,
            "source_rows": source_rows,
            "message": "Database already contains patients; it was not overwritten.",
        }
    report = import_sanitized_workbook(session)
    session.add(AuditLog(
        action="source_database_initialized",
        entity="database",
        new_value=(
            f"{report['therapists']} therapists, {report['patients']} patients, "
            f"{report['requirements']} requirements, {report['warnings']} warnings"
        ),
    ))
    session.commit()
    return {
        "initialized": True,
        **report,
    }
