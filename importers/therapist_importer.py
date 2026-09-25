from __future__ import annotations

from pathlib import Path

import pandas as pd
from sqlalchemy import select

from config import BASE_DIR
from database.models import Department, Location, Therapist, TherapistAvailability
from scheduling.slot_config import SLOTS, WORKING_DAYS


def resolve_csv(*names: str) -> Path:
    for folder in (BASE_DIR / "data", BASE_DIR):
        for name in names:
            candidate = folder / name
            if candidate.exists():
                return candidate
    raise FileNotFoundError(f"Could not find any of: {', '.join(names)}")


def _get_or_create(session, model, name: str, **kwargs):
    item = session.scalar(select(model).where(model.name == name))
    if item is None:
        item = model(name=name, **kwargs)
        session.add(item)
        session.flush()
    return item


def import_source_therapists(session) -> tuple[int, int, Path]:
    """Import only therapist fields present in the supplied source CSV.

    The CSV supplies identity and assignment data. The manager-confirmed
    operating policy supplies Monday-Saturday availability for every active
    therapist, across every configured slot.
    """
    path = resolve_csv("Therapist_Master_Data_25(1).csv", "Therapist_Master_Data_25.csv")
    frame = pd.read_csv(path).fillna("")
    imported = warnings = 0
    for row in frame.to_dict("records"):
        code = str(row["Therapist ID"]).strip()
        if session.scalar(select(Therapist).where(Therapist.therapist_code == code)):
            continue
        department = _get_or_create(session, Department, str(row["Department"]).strip(), active=True)
        location_name = str(row["Location"]).strip()
        location = _get_or_create(
            session,
            Location,
            location_name,
            capacity=0,
            active=True,
        )
        therapist = Therapist(
            therapist_code=code,
            name=str(row["Name"]).strip() or code,
            department_id=department.id,
            location_id=location.id,
            active=str(row["Available Status"]).strip().lower() == "active",
        )
        session.add(therapist)
        session.flush()
        if therapist.active:
            for day in WORKING_DAYS:
                for slot in SLOTS:
                    session.add(TherapistAvailability(
                        therapist_id=therapist.id,
                        day_of_week=day,
                        slot_index=slot.slot_index,
                        available=True,
                    ))
        imported += 1
    for location in session.scalars(select(Location)):
        # There are no rooms: therapist availability is the only real capacity.
        # This redundant ceiling cannot bind before the one-therapist rule does.
        location.capacity = sum(
            1 for therapist in session.scalars(select(Therapist).where(
                Therapist.location_id == location.id,
                Therapist.active.is_(True),
            ))
        )
    return imported, warnings, path
