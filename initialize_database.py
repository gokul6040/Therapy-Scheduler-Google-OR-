"""Create schema and load the sanitized workbook into the configured database."""

from database.db import SessionLocal, init_db
from importers.initialize_source import initialize_source_database


def main() -> None:
    """Initialize an empty configured database without overwriting existing data."""
    init_db()
    with SessionLocal() as session:
        report = initialize_source_database(session)
    print(report)


if __name__ == "__main__":
    main()
