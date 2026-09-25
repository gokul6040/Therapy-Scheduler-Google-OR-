from __future__ import annotations

import re
from pathlib import Path

import pandas as pd
from sqlalchemy import select

from database.models import (
    Department,
    ImportWarning,
    Patient,
    PatientAvailability,
    Therapist,
    TreatmentRequirement,
)
from importers.therapist_importer import resolve_csv
from scheduling.slot_config import SLOTS, WORKING_DAYS

THERAPY_PATTERN = re.compile(r"^(.*?)\s*\((\d+)x\s*/\s*week\)\s*$", re.IGNORECASE)


def parse_therapy(value: str) -> tuple[str, int] | None:
    value = str(value).strip()
    if not value or value.upper() == "N/A":
        return None
    match = THERAPY_PATTERN.match(value)
    if not match:
        raise ValueError(f"Invalid therapy requirement: {value}")
    return match.group(1).strip(), int(match.group(2))


def import_source_patients(session) -> tuple[int, int, Path]:
    """Import source data plus the manager-confirmed open patient availability."""
    path = resolve_csv(
        "Patient_Therapy_Scheduling_Data_1000_corrected.csv",
        "Patient_Therapy_Scheduling_Data_1000(1).csv",
        "Patient_Therapy_Scheduling_Data_1000.csv",
    )
    frame = pd.read_csv(path).fillna("")
    therapy_columns = [f"Therapy {letter} (Frequency)" for letter in "ABCD"]
    imported = warnings = 0
    for row in frame.to_dict("records"):
        patient_code = str(row["Patient ID"]).strip()
        if session.scalar(select(Patient).where(Patient.patient_code == patient_code)):
            continue
        patient = Patient(
            patient_code=patient_code,
            active=True,
            notes=None,
        )
        session.add(patient)
        session.flush()
        for day in WORKING_DAYS:
            for slot in SLOTS:
                session.add(PatientAvailability(
                    patient_id=patient.id,
                    day_of_week=day,
                    slot_index=slot.slot_index,
                    available=True,
                    preference_score=0,
                ))

        assigned_codes = [part.strip() for part in str(row.get("Assigned Therapists", "")).split(",")]
        for sequence, column in enumerate(therapy_columns):
            parsed = parse_therapy(row.get(column, ""))
            if parsed is None:
                continue
            department_name, frequency = parsed
            department = session.scalar(select(Department).where(Department.name == department_name))
            if department is None:
                department = Department(name=department_name, active=True)
                session.add(department)
                session.flush()
            supplied_code = assigned_codes[sequence] if sequence < len(assigned_codes) else ""
            therapist = session.scalar(select(Therapist).where(
                Therapist.therapist_code == supplied_code,
                Therapist.department_id == department.id,
                Therapist.active.is_(True),
            ))
            if therapist is None:
                if not session.scalar(select(Therapist).where(
                    Therapist.department_id == department.id,
                    Therapist.active.is_(True),
                )):
                    message = (
                        f"No active source therapist exists for {department_name}; "
                        "the treatment requirement was not imported."
                    )
                else:
                    message = (
                        f"Assigned therapist '{supplied_code}' is missing, inactive, or does not match "
                        f"{department_name}; the treatment requirement was not imported."
                    )
                session.add(ImportWarning(patient_code=patient_code, message=message))
                warnings += 1
                continue
            session.add(TreatmentRequirement(
                patient_id=patient.id,
                department_id=department.id,
                assigned_therapist_id=therapist.id,
                sessions_per_week=frequency,
                sequence_order=sequence,
                active=True,
            ))
        imported += 1
    return imported, warnings, path
