"""Central configuration for the EIC scheduling prototype."""

from dataclasses import dataclass
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def load_local_env_file(path: Path) -> None:
    """Load local ``KEY=value`` settings without overriding real environment values."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


# ``.env`` is a convenient local-development secret store.  It is ignored by
# Git; deployed environments should set the same values through their secret
# manager instead.
load_local_env_file(BASE_DIR / ".env")

# PostgreSQL is selected by setting THERAPY_SCHEDULER_DATABASE_URL outside the
# repository. Keep SQLite as the safe local fallback until PostgreSQL credentials
# are provisioned. Never commit credentials or an Azure connection string.
LOCAL_DATABASE_URL = f"sqlite:///{(BASE_DIR / 'eic_scheduler_sanitized.db').as_posix()}"
DATABASE_URL = os.environ.get("THERAPY_SCHEDULER_DATABASE_URL", LOCAL_DATABASE_URL)
SOURCE_WORKBOOK = BASE_DIR / "therapy_schedule_sanitized_cleaned.xlsx"
DEMO_USER = "demo_manager"
# The operational schedule is one calendar week: Monday through Saturday.
SCHEDULING_WEEK_DAYS = 6
SOLVER_SEARCH_TIME_OPTIONS = (20.0, 60.0, 120.0, 300.0)

# These are explicitly demo values, not client-approved production capacities.
DEMO_LOCATION_CAPACITIES = {
    "Location A": 10,
    "Location B": 7,
    "Location C": 5,
    "Location D": 3,
    "DEMO Location": 4,
}


@dataclass(frozen=True)
class SolverSettings:
    max_time_seconds: float = 20.0
    random_seed: int = 20260908
    num_search_workers: int = 1  # one worker makes test runs reproducible
    unmet_bundle_penalty: int = 1_000_000
    scheduled_session_reward: int = 10_000
    preference_reward: int = 100
    absence_priority_reward: int = 100_000
    cadence_deviation_penalty: int = 5_000
    confirmed_change_penalty: int = 100_000


SOLVER_SETTINGS = SolverSettings()
