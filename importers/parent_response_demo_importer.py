"""Load explicitly fictional appointments and assumed replies into an empty DB."""

from datetime import date, timedelta
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from database.models import (
    Appointment, AppointmentStatus, Department, Location, ParentAvailabilityResponse,
    Patient, PatientAvailability, Therapist, TreatmentRequirement,
)
from services.parent_response_service import record_parent_response


DEFAULT_FIXTURE = Path(__file__).resolve().parent.parent / "demo" / "parent_responses.json"
LARGE_FIXTURE = DEFAULT_FIXTURE.with_name("parent_responses_large.json")


def load_parent_response_demo(
    session: Session, fixture_path: Path = DEFAULT_FIXTURE,
) -> date:
    """Seed an empty database only; never merge this fixture into existing data."""
    for model in (Patient, Therapist, Appointment, Department, Location, ParentAvailabilityResponse):
        if session.scalar(select(model).limit(1)) is not None:
            raise ValueError("The parent response demo requires an empty database")
    data = json.loads(fixture_path.read_text(encoding="utf-8"))
    target_date = date.fromisoformat(data["target_date"])
    departments = {
        name: Department(name=name)
        for name in data.get("departments", ["Demo Speech Therapy"])
    }
    locations = {
        item["name"]: Location(**item)
        for item in data.get("locations", [{"name": "Demo Branch", "capacity": 2}])
    }
    session.add_all([*departments.values(), *locations.values()])
    session.flush()
    default_department = next(iter(departments.values()))
    default_location = next(iter(locations.values()))
    therapists = {}
    therapist_rows = data.get("therapists") or [
        {"code": code} for code in sorted({item["therapist"] for item in data["patients"]})
    ]
    for item in therapist_rows:
        code = item["code"]
        therapists[code] = Therapist(
            therapist_code=code, name=item.get("name", f"Demo Therapist {code}"),
            department_id=departments.get(item.get("therapy"), default_department).id,
            location_id=locations.get(item.get("branch"), default_location).id, active=True,
        )
    session.add_all(therapists.values())
    session.flush()
    patients = {}
    requirements = {}
    for item in data["patients"]:
        patient = Patient(
            patient_code=item["code"], name=item["name"],
            active=item.get("active", True), notes=item.get("notes"),
        )
        session.add(patient)
        session.flush()
        patients[item["code"]] = patient
        requirement_rows = item.get("requirements") or [{
            "therapy": default_department.name, "therapist": item["therapist"],
            "sessions_per_week": item["sessions_per_week"],
        }]
        for order, row in enumerate(requirement_rows):
            therapist = therapists[row["therapist"]]
            requirements[(item["code"], row["therapy"])] = TreatmentRequirement(
                patient_id=patient.id, department_id=departments[row["therapy"]].id,
                assigned_therapist_id=therapist.id,
                location_id=therapist.location_id,
                sessions_per_week=row["sessions_per_week"],
                sequence_order=order, active=True,
                plan_start_date=date.fromisoformat(row["plan_start_date"])
                if row.get("plan_start_date") else None,
                duration_months=row.get("duration_months"),
            )
    session.add_all(requirements.values())
    session.flush()
    appointments = {}
    for item in data["appointments"]:
        requirement = requirements[(item["patient"], item.get("therapy", default_department.name))]
        appointment = Appointment(
            patient_id=patients[item["patient"]].id,
            treatment_requirement_id=requirement.id,
            therapist_id=requirement.assigned_therapist_id,
            department_id=requirement.department_id, location_id=requirement.location_id,
            date=target_date + timedelta(days=item["day_offset"]),
            slot_index=item["slot"], status=AppointmentStatus(item["status"]),
            bundle_id=f"DEMO-{item.get('bundle', item['key'])}", source="parent_response_demo",
        )
        appointments[item["key"]] = appointment
        session.add(appointment)
    session.flush()
    for item in data.get("patient_unavailable", []):
        session.add(PatientAvailability(
            patient_id=patients[item["patient"]].id,
            day_of_week=item["weekday"], slot_index=item["slot"], available=False,
        ))
    for item in data["responses"]:
        record_parent_response(
            session, event_id=item["event_id"],
            appointment_id=appointments[item["appointment"]].id,
            reply=item["reply"], unavailable_scope=item.get("unavailable_scope"),
        )
    session.commit()
    return target_date
