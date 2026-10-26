from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from config import DATABASE_URL


class Base(DeclarativeBase):
    pass


engine_options = {"future": True}
if not DATABASE_URL.startswith("sqlite"):
    # Azure connections can be interrupted during routine maintenance. Check a
    # checked-out connection before attempting a scheduling transaction.
    engine_options["pool_pre_ping"] = True
engine = create_engine(DATABASE_URL, **engine_options)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    from database import models  # noqa: F401

    Base.metadata.create_all(engine)
    from database.whatsapp_migrations import migrate_whatsapp_button_times

    migrate_whatsapp_button_times(engine)


def init_admin_booking_tables() -> None:
    """Add only reservation tracking; leave existing records and schemas intact."""
    from database.models import AdminBookingDecision, AdminSlotReservation

    AdminBookingDecision.__table__.create(engine, checkfirst=True)
    AdminSlotReservation.__table__.create(engine, checkfirst=True)


def get_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
