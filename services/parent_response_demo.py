"""Run an isolated assumed-reply demo: python -m services.parent_response_demo."""

from __future__ import annotations

import argparse
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from database.db import Base
from database.models import Appointment, Department, ParentAvailabilityResponse, Patient, TreatmentRequirement
from importers.parent_response_demo_importer import load_parent_response_demo
from scheduling.slot_config import SLOTS
from services.parent_response_service import process_daily_responses


def _appointments_table(session: Session) -> str:
    lines = [
        "| Patient | Date | Time | Status |",
        "|---|---|---|---|",
    ]
    for item in session.scalars(select(Appointment).order_by(Appointment.date, Appointment.slot_index, Appointment.id)):
        patient = session.get(Patient, item.patient_id)
        lines.append(
            f"| {patient.patient_code} | {item.date} | "
            f"{SLOTS[item.slot_index].start_time:%H:%M} | {item.status.value} |"
        )
    return "\n".join(lines)


def run_demo(output_dir: Path) -> Path:
    """Create a new SQLite file and a before/after report, without live services."""
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    database_path = output_dir / "parent_response_demo.sqlite"
    report_path = output_dir / "report.md"
    if database_path.exists() or report_path.exists():
        raise ValueError("Demo output already exists; use --output-dir with a new directory")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        Base.metadata.create_all(engine)
        with Session(engine, expire_on_commit=False) as session:
            target_date = load_parent_response_demo(session)
            before = _appointments_table(session)
            replies = ["| Patient | Assumed reply | Scope |", "|---|---|---|"]
            for response in session.scalars(select(ParentAvailabilityResponse).order_by(ParentAvailabilityResponse.event_id)):
                appointment = session.get(Appointment, response.appointment_id)
                patient = session.get(Patient, appointment.patient_id)
                replies.append(f"| {patient.patient_code} | {response.reply} | {response.unavailable_scope or '—'} |")
            result = process_daily_responses(session, target_date)
            after = _appointments_table(session)
            alternatives = ["| Patient | Date | Time |", "|---|---|---|"]
            for item in result.alternatives:
                patient = session.get(Patient, item.patient_id)
                alternatives.append(
                    f"| {patient.patient_code} | {item.date} | {SLOTS[item.slot_index].start_time:%H:%M} |"
                )
            progress = [
                "| Patient | Therapy | Weekly target | Completed | Booked/proposed | Still to arrange |",
                "|---|---|---|---|---|---|",
            ]
            for item in result.weekly_progress:
                patient = session.get(Patient, item.patient_id)
                requirement = session.get(TreatmentRequirement, item.requirement_id)
                department = session.get(Department, requirement.department_id)
                progress.append(
                    f"| {patient.patient_code} | {department.name} | {item.required} | "
                    f"{item.completed} | {item.booked} | {item.remaining} |"
                )
            report = "\n\n".join([
                "# Assumed parent-response scheduling demo",
                f"Target day: **{target_date}**. Every name, appointment, and reply is fictional. "
                "No WhatsApp or Zoho API is called. The configured application database is not used.",
                "## Input replies", "\n".join(replies),
                "C has no response, so its existing visit is unchanged. "
                "The fixture explicitly defines B's No as whole-day unavailability; this is a test input, not a production policy.",
                "## Before", before, "## After", after,
                f"Processed {result.processed} replies; released {len(result.released_appointment_ids)} appointment(s); "
                f"added {len(result.proposed_appointment_ids)} tentative replacement appointment(s). "
                "PROPOSED replacements are not parent-confirmed. All new database appointments are on the target day.",
                "D fills B's opening using the same therapist and branch. "
                "D's existing Tuesday booking stays in place. E already has its weekly requirement booked, "
                "and F requires a different therapist, so neither fills the opening.",
                "## Alternatives for the declining family (not saved or booked)",
                "\n".join(alternatives) if result.alternatives else "No valid alternative was found in the remaining week.",
                "\n".join(
                    f"Unmet requirement {item.requirement_id}: {item.reason}"
                    for item in result.alternative_unmet
                ) or "All declined demand has a valid suggestion in this example.",
                "## Weekly tracking", "\n".join(progress),
                "Booked includes proposed, pending, and confirmed slots. It is separate from completed therapy. "
                "Unsaved alternatives do not reduce the remaining count. "
                "A has one Monday booking against a target of three, leaving two still to arrange.",
                "## Database", f"Local SQLite file: `{database_path}`. "
                "Assumed replies are in `parent_availability_responses`; appointments, absences, and audit events "
                "are in their usual scheduler tables.",
            ])
            report_path.write_text(report + "\n", encoding="utf-8")
    finally:
        engine.dispose()
    return report_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/parent_response_demo"))
    arguments = parser.parse_args()
    report_path = run_demo(arguments.output_dir)
    print(f"Created isolated demo database and report: {report_path}")


if __name__ == "__main__":
    main()
