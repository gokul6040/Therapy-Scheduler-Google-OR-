"""Create an auditable demo-only repair of patient therapist assignments."""

from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path

import pandas as pd

from config import BASE_DIR
from importers.patient_importer import parse_therapy
from importers.therapist_importer import resolve_csv


THERAPY_COLUMNS = tuple(f"Therapy {letter} (Frequency)" for letter in "ABCD")
OUTPUT_NAME = "Patient_Therapy_Scheduling_Data_1000_corrected.csv"


def create_corrected_patient_csv(base_dir: Path = BASE_DIR) -> dict[str, object]:
    """Write a corrected copy using active, department-matched therapists.

    Existing valid assignments remain unchanged. Invalid assignments are repaired
    by selecting the least-loaded active therapist in the matching department;
    therapist code breaks ties. The source value and each change stay in the
    resulting CSV for review.
    """
    source_path = resolve_csv(
        "Patient_Therapy_Scheduling_Data_1000(1).csv",
        "Patient_Therapy_Scheduling_Data_1000.csv",
    )
    therapist_path = resolve_csv("Therapist_Master_Data_25(1).csv", "Therapist_Master_Data_25.csv")
    output_path = base_dir / OUTPUT_NAME

    patients = pd.read_csv(source_path, dtype=str).fillna("")
    therapists = pd.read_csv(therapist_path, dtype=str).fillna("")
    active_by_code = {
        row["Therapist ID"].strip(): row
        for row in therapists.to_dict("records")
        if row["Available Status"].strip().lower() == "active"
    }
    candidates_by_department: dict[str, list[str]] = {}
    for code, row in active_by_code.items():
        candidates_by_department.setdefault(row["Department"].strip(), []).append(code)
    for candidates in candidates_by_department.values():
        candidates.sort()

    weekly_load: Counter[str] = Counter()
    corrected_rows: list[dict[str, str]] = []
    repaired = retained = unresolved = 0
    for row in patients.to_dict("records"):
        original_codes = [part.strip() for part in row.get("Assigned Therapists", "").split(",")]
        corrected_codes = [""] * len(THERAPY_COLUMNS)
        notes: list[str] = []
        for sequence, column in enumerate(THERAPY_COLUMNS):
            parsed = parse_therapy(row.get(column, ""))
            if parsed is None:
                continue
            department, frequency = parsed
            supplied_code = original_codes[sequence] if sequence < len(original_codes) else ""
            supplied = active_by_code.get(supplied_code)
            if supplied and supplied["Department"].strip() == department:
                chosen = supplied_code
                retained += 1
                notes.append(f"{column}: retained {chosen}")
            else:
                candidates = candidates_by_department.get(department, [])
                if not candidates:
                    unresolved += 1
                    notes.append(f"{column}: unresolved; no active {department} therapist")
                    continue
                chosen = min(candidates, key=lambda code: (weekly_load[code], code))
                repaired += 1
                notes.append(f"{column}: {supplied_code or 'blank'} -> {chosen} (balanced department match)")
            corrected_codes[sequence] = chosen
            weekly_load[chosen] += frequency
        corrected = dict(row)
        corrected["Original Assigned Therapists"] = row.get("Assigned Therapists", "")
        corrected["Assigned Therapists"] = ",".join(corrected_codes)
        corrected["Assignment Repair Notes"] = " | ".join(notes)
        corrected_rows.append(corrected)

    fieldnames = list(patients.columns)
    fieldnames.insert(fieldnames.index("Assigned Therapists"), "Original Assigned Therapists")
    fieldnames.append("Assignment Repair Notes")
    with output_path.open("w", newline="", encoding="utf-8") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(corrected_rows)
    return {
        "source": source_path,
        "output": output_path,
        "retained": retained,
        "repaired": repaired,
        "unresolved": unresolved,
        "weekly_load": dict(sorted(weekly_load.items())),
    }
