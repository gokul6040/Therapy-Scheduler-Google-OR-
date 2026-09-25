from datetime import date

import pandas as pd
import pytest
from sqlalchemy import func, select

from database.models import (
    ImportWarning,
    Location,
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
from database.repository import load_scheduling_problem
from importers import patient_importer, therapist_importer
from importers.patient_assignment_repair import create_corrected_patient_csv
from importers.patient_importer import parse_therapy
from importers.workbook_importer import import_sanitized_workbook


def test_parse_therapy():
    assert parse_therapy("Speech and Language Therapy (3x / week)") == ("Speech and Language Therapy", 3)
    assert parse_therapy("N/A") is None
    with pytest.raises(ValueError):
        parse_therapy("Speech sometimes")


def test_source_import_applies_manager_confirmed_availability(db_session, monkeypatch, tmp_path):
    therapist_file = tmp_path / "therapists.csv"
    therapist_file.write_text(
        "Therapist ID,Department,Location,Name,Available Status\n"
        "T-01,Speech,Clinic A,Dr One,Active\n",
        encoding="utf-8",
    )
    patient_file = tmp_path / "patients.csv"
    patient_file.write_text(
        "Patient ID,Assigned Therapists,Therapy A (Frequency),Therapy B (Frequency),Therapy C (Frequency),Therapy D (Frequency)\n"
        "P-01,T-01,Speech (2x / week),N/A,N/A,N/A\n"
        "P-02,UNKNOWN,Speech (1x / week),N/A,N/A,N/A\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(therapist_importer, "resolve_csv", lambda *_: therapist_file)
    monkeypatch.setattr(patient_importer, "resolve_csv", lambda *_: patient_file)

    therapist_importer.import_source_therapists(db_session)
    patients, warnings, _ = patient_importer.import_source_patients(db_session)
    db_session.commit()

    assert patients == 2
    assert warnings == 1  # invalid assignment only
    assert db_session.scalar(select(func.count(TreatmentRequirement.id))) == 1
    assert db_session.scalar(select(func.count(PatientAvailability.id))) == 120
    assert db_session.scalar(select(func.count(TherapistAvailability.id))) == 60
    assert db_session.scalar(select(Location.capacity)) == 1
    assert db_session.scalar(select(func.count(ImportWarning.id))) == 1


def test_assignment_repair_balances_invalid_assignments(monkeypatch, tmp_path):
    therapist_file = tmp_path / "therapists.csv"
    therapist_file.write_text(
        "Therapist ID,Department,Location,Name,Available Status\n"
        "T-01,Speech,A,One,Active\n"
        "T-02,Speech,A,Two,Active\n",
        encoding="utf-8",
    )
    patient_file = tmp_path / "patients.csv"
    patient_file.write_text(
        "Patient ID,Assigned Therapists,Therapy A (Frequency),Therapy B (Frequency),Therapy C (Frequency),Therapy D (Frequency)\n"
        "P-01,BAD,Speech (2x / week),N/A,N/A,N/A\n"
        "P-02,BAD,Speech (2x / week),N/A,N/A,N/A\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "importers.patient_assignment_repair.resolve_csv",
        lambda *names: therapist_file if "Therapist_Master_Data_25.csv" in names else patient_file,
    )

    report = create_corrected_patient_csv(tmp_path)
    rows = list(__import__("csv").DictReader((tmp_path / "Patient_Therapy_Scheduling_Data_1000_corrected.csv").open()))

    assert report["repaired"] == 2
    assert report["unresolved"] == 0
    assert [row["Assigned Therapists"] for row in rows] == ["T-01,,,", "T-02,,,"]
    assert [row["Original Assigned Therapists"] for row in rows] == ["BAD", "BAD"]


def test_sanitized_workbook_uses_plan_detail_and_master_data(db_session, tmp_path):
    workbook = tmp_path / "therapy_schedule_sanitized_cleaned.xlsx"
    with pd.ExcelWriter(workbook) as writer:
        pd.DataFrame([
            {"Therapy Type": "Speech Therapy Session", "Specialty": "Speech & Language Therapy"},
            {"Therapy Type": "IEP Session", "Specialty": "Special Education / IEP"},
        ]).to_excel(writer, sheet_name="Specialty_Master", index=False)
        pd.DataFrame([
            {
                "Therapist ID": "T-SPEECH", "Therapist Name": "Speech One", "Branch(es)": "Clinic A",
                "Specialties": "Speech & Language Therapy", "Therapy Types": "Speech Therapy Session",
            },
            {
                "Therapist ID": "T-MULTI", "Therapist Name": "Multi One", "Branch(es)": "Clinic A",
                "Specialties": "Speech & Language Therapy; Special Education / IEP",
                "Therapy Types": "Speech Therapy Session; IEP Session",
            },
            {
                "Therapist ID": "T-OTHER", "Therapist Name": "Other Branch", "Branch(es)": "Clinic B",
                "Specialties": "Speech & Language Therapy", "Therapy Types": "Speech Therapy Session",
            },
        ]).to_excel(writer, sheet_name="Therapist_Master", index=False)
        pd.DataFrame([
            {
                "Registration Id": "P-01", "Name": "Child One", "Branch": "Clinic A",
                "Therapy Type": "Speech Therapy Session", "Specialty": "Speech Alias",
                "Weekly Frequency": 2, "Duration Months": 3, "Therapist": "Speech One",
                "Start Date": "2026-09-01", "Plan Added Time": "2026-09-01 09:00", "Status": "Draft",
            },
            {
                "Registration Id": "P-01", "Name": "Child One", "Branch": "Clinic A",
                "Therapy Type": "Speech Therapy Session", "Specialty": "Speech & Language Therapy",
                "Weekly Frequency": 6, "Duration Months": 3, "Therapist": "Speech One",
                "Start Date": "2026-08-01", "Plan Added Time": "2026-08-01 09:00", "Status": "Draft",
            },
            {
                "Registration Id": "P-01", "Name": "Child One", "Branch": "Clinic A",
                "Therapy Type": "IEP Session", "Specialty": "Special Education / IEP",
                "Weekly Frequency": 1, "Duration Months": 3, "Therapist": "Multi One",
                "Start Date": "2026-09-01", "Plan Added Time": "2026-09-01 09:00", "Status": "Draft",
            },
            {
                "Registration Id": "P-02", "Name": "Child Two", "Branch": "Clinic A",
                "Therapy Type": "Speech Therapy Session", "Specialty": "Speech & Language Therapy",
                "Weekly Frequency": 0, "Duration Months": 3, "Therapist": "Speech One",
                "Start Date": "2026-09-01", "Plan Added Time": "2026-09-01 09:00", "Status": "Draft",
            },
        ]).to_excel(writer, sheet_name="Plan_Detail", index=False)

    report = import_sanitized_workbook(db_session, workbook)
    db_session.commit()

    assert report["therapists"] == 3
    assert report["patients"] == 1
    assert report["requirements"] == 2
    assert report["warnings"] == 2
    assert report["source_rows"] == {
        "plan_detail": 4,
        "therapist_master": 3,
        "specialty_master": 2,
    }
    assert db_session.scalar(select(func.count(PlanDetailSource.source_row_number))) == 4
    assert db_session.scalar(select(func.count(TherapistMasterSource.source_row_number))) == 3
    assert db_session.scalar(select(func.count(SpecialtyMasterSource.source_row_number))) == 2
    assert db_session.scalar(select(func.count(TherapistCapability.id))) == 4
    assert db_session.scalar(select(func.count(TherapistLocation.id))) == 3
    requirements = list(db_session.scalars(select(TreatmentRequirement).order_by(TreatmentRequirement.id)))
    assert requirements[0].specialty_name == "Speech & Language Therapy"
    assert requirements[0].duration_months == 3
    assert requirements[0].plan_start_date.isoformat() == "2026-09-01"

    problem = load_scheduling_problem(db_session, date(2026, 9, 7))
    speech_requirement = next(item for item in problem.requirements if item.sessions_per_week == 2)
    assert speech_requirement.location_id == db_session.scalar(
        select(Location.id).where(Location.name == "Clinic A")
    )
    eligible_codes = {
        therapist.therapist_code
        for therapist in db_session.scalars(
            select(Therapist).where(
                Therapist.id.in_(problem.eligible_therapists[speech_requirement.id])
            )
        )
    }
    assert eligible_codes == {"T-SPEECH"}
