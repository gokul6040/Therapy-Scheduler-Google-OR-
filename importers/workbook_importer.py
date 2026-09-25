"""Import the sanitized current-plan workbook used by the active scheduler."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import insert, select

from config import SOURCE_WORKBOOK
from database.models import (
    Department,
    ImportWarning,
    Location,
    Patient,
    PatientAvailability,
    PlanDetailSource,
    SpecialtyMasterSource,
    Therapist,
    TherapistAvailability,
    TherapistCapability,
    TherapistLocation,
    TherapistMasterSource,
    TreatmentRequirement,
)
from scheduling.slot_config import SLOTS, WORKING_DAYS


PLAN_DETAIL_SHEET = "Plan_Detail"
THERAPIST_MASTER_SHEET = "Therapist_Master"
SPECIALTY_MASTER_SHEET = "Specialty_Master"

PLAN_COLUMNS = {
    "Registration Id", "Name", "Branch", "Therapy Type", "Specialty",
    "Weekly Frequency", "Duration Months", "Therapist", "Start Date",
    "Plan Added Time", "Status",
}
THERAPIST_COLUMNS = {
    "Therapist ID", "Therapist Name", "Branch(es)", "Specialties", "Therapy Types",
}
SPECIALTY_COLUMNS = {"Therapy Type", "Specialty"}


def _text(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return " ".join(str(value).strip().split())


def _key(value: Any) -> str:
    return _text(value).casefold()


def _parts(value: Any) -> list[str]:
    return [item for item in (_text(part) for part in _text(value).split(";")) if item]


def _optional_date(value: Any) -> date | None:
    if value is None or pd.isna(value):
        return None
    return pd.Timestamp(value).date()


def _optional_datetime(value: Any) -> datetime | None:
    if value is None or pd.isna(value):
        return None
    return pd.Timestamp(value).to_pydatetime()


def _positive_int(value: Any) -> int | None:
    if value is None or pd.isna(value):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not number.is_integer() or number <= 0:
        return None
    return int(number)


def _warning(session, patient_code: str | None, message: str) -> None:
    session.add(ImportWarning(patient_code=patient_code, message=message))


def _read_sheets(path: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    if not path.exists():
        raise FileNotFoundError(f"Sanitized source workbook was not found: {path}")
    sheets = pd.read_excel(
        path,
        sheet_name=[PLAN_DETAIL_SHEET, THERAPIST_MASTER_SHEET, SPECIALTY_MASTER_SHEET],
    )
    plan_detail = sheets[PLAN_DETAIL_SHEET]
    therapist_master = sheets[THERAPIST_MASTER_SHEET]
    specialty_master = sheets[SPECIALTY_MASTER_SHEET]
    missing = {
        PLAN_DETAIL_SHEET: PLAN_COLUMNS - set(plan_detail.columns),
        THERAPIST_MASTER_SHEET: THERAPIST_COLUMNS - set(therapist_master.columns),
        SPECIALTY_MASTER_SHEET: SPECIALTY_COLUMNS - set(specialty_master.columns),
    }
    invalid = {sheet: sorted(columns) for sheet, columns in missing.items() if columns}
    if invalid:
        raise ValueError(f"Sanitized workbook is missing required columns: {invalid}")
    return plan_detail, therapist_master, specialty_master


def load_authoritative_source_tables(
    session,
    path: Path = SOURCE_WORKBOOK,
) -> dict[str, int]:
    """Copy the three approved workbook sheets into their visible DB tables.

    The source tables are immutable snapshots for a normal app start.  A
    deliberate source refresh is handled separately so active appointments are
    never overwritten incidentally.
    """
    plan_detail, therapist_master, specialty_master = _read_sheets(path)
    if session.scalar(select(PlanDetailSource.source_row_number).limit(1)) is not None:
        return {"plan_detail": 0, "therapist_master": 0, "specialty_master": 0}

    def value(row: dict[str, Any], column: str) -> str | None:
        return _text(row.get(column)) or None

    session.execute(insert(PlanDetailSource), [
        {
            "source_row_number": number,
            "registration_id": value(row, "Registration Id"),
            "name": value(row, "Name"),
            "branch": value(row, "Branch"),
            "therapy_type": value(row, "Therapy Type"),
            "specialty": value(row, "Specialty"),
            "weekly_frequency": value(row, "Weekly Frequency"),
            "duration_months": value(row, "Duration Months"),
            "therapist": value(row, "Therapist"),
            "therapist_source": value(row, "Therapist Source"),
            "start_date": _optional_date(row.get("Start Date")),
            "plan_added_time": _optional_datetime(row.get("Plan Added Time")),
            "status": value(row, "Status"),
            "quality_flag": value(row, "Quality Flag"),
        }
        for number, row in enumerate(plan_detail.to_dict("records"), start=2)
    ])
    session.execute(insert(TherapistMasterSource), [
        {
            "source_row_number": number,
            "therapist_code": value(row, "Therapist ID"),
            "therapist_name": value(row, "Therapist Name"),
            "branches": value(row, "Branch(es)"),
            "specialties": value(row, "Specialties"),
            "therapy_types": value(row, "Therapy Types"),
            "source_type": value(row, "Source Type"),
            "used_in_current_plans": value(row, "Used in Current Plans"),
            "current_patients": value(row, "Current Patients"),
            "current_plan_lines": value(row, "Current Plan Lines"),
            "historical_source_lines": value(row, "Historical Source Lines"),
        }
        for number, row in enumerate(therapist_master.to_dict("records"), start=2)
    ])
    session.execute(insert(SpecialtyMasterSource), [
        {
            "source_row_number": number,
            "therapy_type": value(row, "Therapy Type"),
            "specialty": value(row, "Specialty"),
            "short_code": value(row, "Short Code"),
            "typical_capability_area": value(row, "Typical Capability / Area"),
        }
        for number, row in enumerate(specialty_master.to_dict("records"), start=2)
    ])
    return {
        "plan_detail": len(plan_detail),
        "therapist_master": len(therapist_master),
        "specialty_master": len(specialty_master),
    }


def read_authoritative_source_tables(
    session,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Build scheduler inputs from the persisted source tables, not Excel."""
    plan_detail = pd.DataFrame([
        {
            "Registration Id": row.registration_id,
            "Name": row.name,
            "Branch": row.branch,
            "Therapy Type": row.therapy_type,
            "Specialty": row.specialty,
            "Weekly Frequency": row.weekly_frequency,
            "Duration Months": row.duration_months,
            "Therapist": row.therapist,
            "Therapist Source": row.therapist_source,
            "Start Date": row.start_date,
            "Plan Added Time": row.plan_added_time,
            "Status": row.status,
            "Quality Flag": row.quality_flag,
        }
        for row in session.scalars(
            select(PlanDetailSource).order_by(PlanDetailSource.source_row_number)
        )
    ])
    therapist_master = pd.DataFrame([
        {
            "Therapist ID": row.therapist_code,
            "Therapist Name": row.therapist_name,
            "Branch(es)": row.branches,
            "Specialties": row.specialties,
            "Therapy Types": row.therapy_types,
            "Source Type": row.source_type,
            "Used in Current Plans": row.used_in_current_plans,
            "Current Patients": row.current_patients,
            "Current Plan Lines": row.current_plan_lines,
            "Historical Source Lines": row.historical_source_lines,
        }
        for row in session.scalars(
            select(TherapistMasterSource).order_by(TherapistMasterSource.source_row_number)
        )
    ])
    specialty_master = pd.DataFrame([
        {
            "Therapy Type": row.therapy_type,
            "Specialty": row.specialty,
            "Short Code": row.short_code,
            "Typical Capability / Area": row.typical_capability_area,
        }
        for row in session.scalars(
            select(SpecialtyMasterSource).order_by(SpecialtyMasterSource.source_row_number)
        )
    ])
    return plan_detail, therapist_master, specialty_master


def _get_or_create(session, model, name: str, **kwargs):
    item = session.scalar(select(model).where(model.name == name))
    if item is None:
        item = model(name=name, **kwargs)
        session.add(item)
        session.flush()
    return item


def _insert_in_batches(session, model, rows: list[dict[str, object]], batch_size: int = 5_000) -> None:
    """Insert generated availability efficiently on remote database connections."""
    for offset in range(0, len(rows), batch_size):
        session.execute(insert(model), rows[offset:offset + batch_size])


def _specialty_map(specialty_master: pd.DataFrame) -> dict[str, tuple[str, str]]:
    """Return normalized therapy type -> canonical therapy type and specialty."""
    mappings: dict[str, tuple[str, str]] = {}
    for row in specialty_master.to_dict("records"):
        therapy_type = _text(row["Therapy Type"])
        specialty = _text(row["Specialty"])
        if not therapy_type or not specialty:
            raise ValueError("Specialty_Master contains a blank Therapy Type or Specialty")
        existing = mappings.setdefault(_key(therapy_type), (therapy_type, specialty))
        if existing != (therapy_type, specialty):
            raise ValueError(f"Specialty_Master has conflicting mappings for {therapy_type!r}")
    return mappings


def _latest_plan_rows(plan_detail: pd.DataFrame) -> pd.DataFrame:
    """Keep all therapy rows from the latest known plan for each child.

    A plan version can contain several therapy rows, so this deliberately
    selects the latest *plan timestamp* per registration ID rather than one
    latest row per therapy.  Start date supplies a deterministic fallback when
    the source does not have a plan-added timestamp.
    """
    latest_versions: dict[str, tuple[datetime, datetime]] = {}
    row_versions: dict[int, tuple[datetime, datetime]] = {}
    for index, row in plan_detail.iterrows():
        registration_id = _key(row["Registration Id"])
        if not registration_id:
            continue
        start = _optional_date(row["Start Date"])
        version = (
            _optional_datetime(row["Plan Added Time"]) or datetime.min,
            datetime.combine(start, time.min) if start else datetime.min,
        )
        row_versions[index] = version
        if version > latest_versions.get(registration_id, (datetime.min, datetime.min)):
            latest_versions[registration_id] = version
    return plan_detail.loc[
        [
            index
            for index, row in plan_detail.iterrows()
            if _key(row["Registration Id"])
            and row_versions.get(index) == latest_versions.get(_key(row["Registration Id"]))
        ]
    ]


def import_sanitized_workbook(session, path: Path = SOURCE_WORKBOOK) -> dict[str, object]:
    """Import current plans, therapist capabilities, and branch memberships.

    The workbook seeds the three authoritative source tables once.  The
    scheduler projection is then built from ``plan_detail``,
    ``therapist_master``, and ``specialty_master`` in the database.
    """
    source_rows = load_authoritative_source_tables(session, path)
    plan_detail, therapist_master, specialty_master = read_authoritative_source_tables(session)
    specialties = _specialty_map(specialty_master)
    departments: dict[str, Department] = {}
    locations: dict[str, Location] = {}

    def department_for(therapy_type: str) -> Department:
        canonical_therapy, _ = specialties[_key(therapy_type)]
        item = departments.get(canonical_therapy)
        if item is None:
            item = _get_or_create(session, Department, canonical_therapy, active=True)
            departments[canonical_therapy] = item
        return item

    def location_for(branch: str) -> Location:
        item = locations.get(branch)
        if item is None:
            item = _get_or_create(session, Location, branch, capacity=0, active=True)
            locations[branch] = item
        return item

    therapists_by_name: dict[str, Therapist] = {}
    therapist_therapy_types: dict[str, set[str]] = {}
    therapist_branches: dict[str, set[str]] = {}
    pending_therapist_details: list[tuple[Therapist, list[str], list[str]]] = []
    existing_therapist_codes = set(session.scalars(select(Therapist.therapist_code)))
    imported_therapists = warnings = 0
    for row in therapist_master.to_dict("records"):
        code = _text(row["Therapist ID"])
        name = _text(row["Therapist Name"])
        branches = _parts(row["Branch(es)"])
        therapy_types = _parts(row["Therapy Types"])
        supplied_specialties = {_key(value) for value in _parts(row["Specialties"])}
        if not code or not name or not branches or not therapy_types:
            _warning(session, None, f"Therapist_Master row is incomplete for therapist {code or name or 'unknown'}")
            warnings += 1
            continue
        if code in existing_therapist_codes:
            continue
        unknown_types = [item for item in therapy_types if _key(item) not in specialties]
        if unknown_types:
            _warning(session, None, f"Therapist {code} has therapy types absent from Specialty_Master: {unknown_types}")
            warnings += 1
            continue
        canonical_types = [specialties[_key(item)][0] for item in therapy_types]
        expected_specialties = {_key(specialties[_key(item)][1]) for item in therapy_types}
        if supplied_specialties and supplied_specialties != expected_specialties:
            _warning(session, None, f"Therapist {code} specialties were normalized from Specialty_Master")
            warnings += 1
        primary_department = department_for(canonical_types[0])
        primary_location = location_for(branches[0])
        therapist = Therapist(
            therapist_code=code,
            name=name,
            department_id=primary_department.id,
            location_id=primary_location.id,
            active=True,
        )
        session.add(therapist)
        pending_therapist_details.append((therapist, canonical_types, branches))
        therapist_key = _key(name)
        therapists_by_name[therapist_key] = therapist
        therapist_therapy_types[therapist_key] = {_key(item) for item in canonical_types}
        therapist_branches[therapist_key] = {_key(item) for item in branches}
        imported_therapists += 1

    session.flush()
    capability_rows: list[dict[str, object]] = []
    therapist_location_rows: list[dict[str, object]] = []
    therapist_availability_rows: list[dict[str, object]] = []
    for therapist, canonical_types, branches in pending_therapist_details:
        for therapy_type in dict.fromkeys(canonical_types):
            capability_rows.append({
                "therapist_id": therapist.id,
                "department_id": department_for(therapy_type).id,
            })
        for branch in dict.fromkeys(branches):
            therapist_location_rows.append({
                "therapist_id": therapist.id,
                "location_id": location_for(branch).id,
            })
        for day in WORKING_DAYS:
            for slot in SLOTS:
                therapist_availability_rows.append({
                    "therapist_id": therapist.id,
                    "day_of_week": day,
                    "slot_index": slot.slot_index,
                    "available": True,
                })

    _insert_in_batches(session, TherapistCapability, capability_rows)
    _insert_in_batches(session, TherapistLocation, therapist_location_rows)
    _insert_in_batches(session, TherapistAvailability, therapist_availability_rows)

    for location in session.scalars(select(Location)):
        location.capacity = len({
            item.therapist_id
            for item in session.scalars(select(TherapistLocation).where(TherapistLocation.location_id == location.id))
        })

    # Read existing patients once.  Looking them up once per plan row causes
    # SQLAlchemy to flush all pending availability rows repeatedly, which is
    # prohibitively slow over a hosted PostgreSQL connection.
    patients_by_code = {
        patient.patient_code: patient
        for patient in session.scalars(select(Patient))
    }
    sequence_by_patient: defaultdict[int, int] = defaultdict(int)
    new_patients: list[Patient] = []
    seen_exact_rows: set[tuple[object, ...]] = set()
    imported_requirements = imported_patients = 0
    for row in _latest_plan_rows(plan_detail).to_dict("records"):
        patient_code = _text(row["Registration Id"])
        patient_name = _text(row["Name"])
        branch = _text(row["Branch"])
        therapy_type = _text(row["Therapy Type"])
        specialty = _text(row["Specialty"])
        therapist_name = _text(row["Therapist"])
        exact_row = tuple(_text(row.get(column)) for column in plan_detail.columns)
        if exact_row in seen_exact_rows:
            _warning(session, patient_code or None, "Exact duplicate Plan_Detail row was ignored")
            warnings += 1
            continue
        seen_exact_rows.add(exact_row)
        if not patient_code or not branch or not therapy_type or not therapist_name:
            _warning(session, patient_code or None, "Plan_Detail row is missing a required scheduling field")
            warnings += 1
            continue
        mapping = specialties.get(_key(therapy_type))
        if mapping is None:
            _warning(session, patient_code, f"Therapy type {therapy_type!r} is absent from Specialty_Master")
            warnings += 1
            continue
        canonical_therapy, canonical_specialty = mapping
        if specialty and _key(specialty) != _key(canonical_specialty):
            _warning(session, patient_code, f"Specialty for {canonical_therapy} was normalized from Specialty_Master")
            warnings += 1
        frequency = _positive_int(row["Weekly Frequency"])
        if frequency is None:
            _warning(session, patient_code, f"Non-positive or invalid weekly frequency for {canonical_therapy}; row was ignored")
            warnings += 1
            continue
        therapist = therapists_by_name.get(_key(therapist_name))
        if therapist is None:
            _warning(session, patient_code, f"Assigned therapist {therapist_name!r} is absent from Therapist_Master")
            warnings += 1
            continue
        department = department_for(canonical_therapy)
        location = location_for(branch)
        if (
            _key(canonical_therapy) not in therapist_therapy_types[_key(therapist_name)]
            or _key(branch) not in therapist_branches[_key(therapist_name)]
        ):
            _warning(
                session,
                patient_code,
                f"Assigned therapist {therapist_name!r} is not qualified for {canonical_therapy} at {branch}",
            )
            warnings += 1
            continue
        patient = patients_by_code.get(patient_code)
        if patient is None:
            patient = Patient(patient_code=patient_code, name=patient_name or None, active=True)
            session.add(patient)
            new_patients.append(patient)
            imported_patients += 1
            patients_by_code[patient_code] = patient
        duration = _positive_int(row["Duration Months"])
        if _text(row["Duration Months"]) and duration is None:
            _warning(session, patient_code, f"Invalid duration retained as blank for {canonical_therapy}")
            warnings += 1
        plan_start = _optional_date(row["Start Date"])
        if plan_start is None:
            _warning(session, patient_code, f"Missing plan start date for {canonical_therapy}")
            warnings += 1
        session.add(TreatmentRequirement(
            patient=patient,
            department_id=department.id,
            assigned_therapist_id=therapist.id,
            location_id=location.id,
            sessions_per_week=frequency,
            sequence_order=sequence_by_patient[patient_code],
            specialty_name=canonical_specialty,
            duration_months=duration,
            plan_start_date=plan_start,
            plan_added_at=_optional_datetime(row["Plan Added Time"]),
            source_status=_text(row["Status"]) or None,
            active=True,
        ))
        sequence_by_patient[patient_code] += 1
        imported_requirements += 1

    # Requirements reference Patient objects, so one flush assigns all IDs
    # before generating default availability in large database batches.
    session.flush()
    patient_availability_rows = [
        {
            "patient_id": patient.id,
            "day_of_week": day,
            "slot_index": slot.slot_index,
            "available": True,
            "preference_score": 0,
        }
        for patient in new_patients
        for day in WORKING_DAYS
        for slot in SLOTS
    ]
    _insert_in_batches(session, PatientAvailability, patient_availability_rows)

    return {
        "therapists": imported_therapists,
        "patients": imported_patients,
        "requirements": imported_requirements,
        "warnings": warnings,
        "source_rows": source_rows,
        "source_file": str(path),
    }
