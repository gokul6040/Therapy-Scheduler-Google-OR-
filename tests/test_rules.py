from datetime import date, time

from scheduling.schedule_validator import validate_schedule
from scheduling.slot_config import SESSION_DURATION_MINUTES, SLOTS, WORKING_DAYS, valid_consecutive_block
from scheduling.types import ScheduledAppointment
from tests.conftest import req


def test_slot_configuration():
    assert SESSION_DURATION_MINUTES == 40
    assert WORKING_DAYS == (0, 1, 2, 3, 4, 5)
    assert valid_consecutive_block(1, 2)
    assert not valid_consecutive_block(4, 2)  # lunch breaks a sequence


def test_lunch_is_1235_to_1315_and_afternoon_can_start_at_1315():
    lunch_start, lunch_end = time(12, 35), time(13, 15)
    assert SLOTS[4].end_time == lunch_start
    assert SLOTS[5].start_time == lunch_end
    assert all(slot.end_time <= lunch_start or slot.start_time >= lunch_end for slot in SLOTS)
    assert not valid_consecutive_block(4, 2)
    assert valid_consecutive_block(5, 2)


def test_wrong_therapist_is_rejected(base_problem):
    requirement = req(1, 1, 10, 20)
    problem = base_problem([requirement])
    appointment = ScheduledAppointment(1, 1, 99, 10, 1, date(2026, 9, 7), 0, "B1")
    _, report = validate_schedule(problem, [appointment])
    assert report["wrong_therapist"] == 1
