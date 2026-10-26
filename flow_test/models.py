"""Queue and offer state used only by the isolated test database."""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Keep test-only tables out of the production metadata and migrations."""

    pass


class FlowSetting(Base):
    __tablename__ = "flow_test_settings"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class FlowContactWindow(Base):
    """Verified inbound time for one coworker and receiving business number."""

    __tablename__ = "flow_test_contact_windows"
    recipient: Mapped[str] = mapped_column(String(24), primary_key=True)
    business_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    last_inbound: Mapped[datetime] = mapped_column(DateTime)


class FlowOffer(Base):
    __tablename__ = "flow_test_offers"
    token: Mapped[str] = mapped_column(String(40), primary_key=True)
    appointment_ids: Mapped[str] = mapped_column(Text)
    snapshot: Mapped[str] = mapped_column(Text)
    patient_code: Mapped[str] = mapped_column(String(40))
    recipient: Mapped[str] = mapped_column(String(24))
    state: Mapped[str] = mapped_column(String(30), default="PENDING")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)


class FlowJob(Base):
    __tablename__ = "flow_test_jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_key: Mapped[str] = mapped_column(String(128), unique=True)
    kind: Mapped[str] = mapped_column(String(40))
    payload: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="QUEUED", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    message_id: Mapped[str | None] = mapped_column(String(200), index=True)
    delivery: Mapped[str | None] = mapped_column(String(30))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class FlowInbox(Base):
    __tablename__ = "flow_test_inbox"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_key: Mapped[str] = mapped_column(String(128), unique=True)
    patient_code: Mapped[str | None] = mapped_column(String(40))
    reason: Mapped[str] = mapped_column(String(100))
    text: Mapped[str] = mapped_column(Text)
    resolved: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
