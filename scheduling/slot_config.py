from dataclasses import dataclass
from datetime import time

SESSION_DURATION_MINUTES = 40
WORKING_DAYS = (0, 1, 2, 3, 4, 5)  # Monday through Saturday


@dataclass(frozen=True)
class Slot:
    slot_index: int
    start_time: time
    end_time: time


SLOTS = (
    Slot(0, time(9, 0), time(9, 40)),
    Slot(1, time(9, 40), time(10, 20)),
    Slot(2, time(10, 35), time(11, 15)),
    Slot(3, time(11, 15), time(11, 55)),
    Slot(4, time(11, 55), time(12, 35)),
    Slot(5, time(13, 15), time(13, 55)),
    Slot(6, time(13, 55), time(14, 35)),
    Slot(7, time(14, 35), time(15, 15)),
    Slot(8, time(15, 30), time(16, 10)),
    Slot(9, time(16, 10), time(16, 50)),
)

# A short operational break can remain consecutive; lunch cannot.
CONSECUTIVE_TO_NEXT = {
    0: True,
    1: True,
    2: True,
    3: True,
    4: False,
    5: True,
    6: True,
    7: True,
    8: True,
    9: False,
}


def valid_consecutive_block(start_slot: int, length: int) -> bool:
    if length < 1 or start_slot < 0 or start_slot + length > len(SLOTS):
        return False
    return all(CONSECUTIVE_TO_NEXT[index] for index in range(start_slot, start_slot + length - 1))


def slot_label(slot_index: int) -> str:
    slot = SLOTS[slot_index]
    return f"{slot.start_time:%H:%M}–{slot.end_time:%H:%M}"
