from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.db import Base
from scheduling.types import RequirementInput, SchedulingProblem


@pytest.fixture
def base_problem():
    def factory(requirements, therapist_availability=None, patient_availability=None, capacities=None):
        therapists = {item.therapist_id for item in requirements}
        return SchedulingProblem(
            week_start=date(2026, 9, 7),
            requirements=requirements,
            therapist_availability=therapist_availability if therapist_availability is not None else {
                (therapist, day, slot)
                for therapist in therapists for day in range(6) for slot in range(10)
            },
            patient_availability=patient_availability if patient_availability is not None else {
                (patient, day, slot)
                for patient in {item.patient_id for item in requirements}
                for day in range(6) for slot in range(10)
            },
            location_capacities=capacities if capacities is not None else {1: 10},
            active_therapists=therapists,
        )
    return factory


def req(identifier, patient, department, therapist, frequency=1, order=0, location=1):
    return RequirementInput(identifier, patient, department, therapist, location, frequency, order)


@pytest.fixture
def db_session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
