from datetime import date

from scheduling.schedule_validator import validate_schedule
from scheduling.slot_config import SESSION_DURATION_MINUTES, WORKING_DAYS, valid_consecutive_block
from scheduling.types import ScheduledAppointment
from tests.conftest import req


def test_slot_configuration():
    assert SESSION_DURATION_MINUTES == 40
    assert WORKING_DAYS == (0, 1, 2, 3, 4, 5)
    assert valid_consecutive_block(1, 2)
    assert not valid_consecutive_block(4, 2)  # lunch breaks a sequence


def test_wrong_therapist_is_rejected(base_problem):
    requirement = req(1, 1, 10, 20)
    problem = base_problem([requirement])
    appointment = ScheduledAppointment(1, 1, 99, 10, 1, date(2026, 9, 7), 0, "B1")
    _, report = validate_schedule(problem, [appointment])
    assert report["wrong_therapist"] == 1
