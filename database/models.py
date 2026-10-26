from __future__ import annotations

import enum
from datetime import date, datetime

from sqlalchemy import Boolean, Computed, Date, DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint, literal_column
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.db import Base
from database.whatsapp_time import KolkataTimestamp, KolkataWallTime, WHATSAPP_TIME_ZONE, kolkata_now


class AppointmentStatus(str, enum.Enum):
    PROPOSED = "PROPOSED"
    PENDING_CONFIRMATION = "PENDING_CONFIRMATION"
    CONFIRMED = "CONFIRMED"
    CANCELLED_PATIENT = "CANCELLED_PATIENT"
    CANCELLED_OTHER = "CANCELLED_OTHER"
    NEEDS_RESCHEDULING = "NEEDS_RESCHEDULING"
    COMPLETED = "COMPLETED"
    NO_SHOW = "NO_SHOW"


class Department(Base):
    __tablename__ = "departments"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Location(Base):
    __tablename__ = "locations"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    capacity: Mapped[int] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Therapist(Base):
    __tablename__ = "therapists"
    id: Mapped[int] = mapped_column(primary_key=True)
    therapist_code: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id"))
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    department: Mapped[Department] = relationship()
    location: Mapped[Location] = relationship()


class TherapistCapability(Base):
    """A therapy type that a therapist is qualified to provide."""

    __tablename__ = "therapist_capabilities"
    __table_args__ = (UniqueConstraint("therapist_id", "department_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    therapist_id: Mapped[int] = mapped_column(ForeignKey("therapists.id"), index=True)
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id"), index=True)


class TherapistLocation(Base):
    """A branch where a therapist may be scheduled."""

    __tablename__ = "therapist_locations"
    __table_args__ = (UniqueConstraint("therapist_id", "location_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    therapist_id: Mapped[int] = mapped_column(ForeignKey("therapists.id"), index=True)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), index=True)


class TherapistAvailability(Base):
    __tablename__ = "therapist_availability"
    __table_args__ = (UniqueConstraint("therapist_id", "day_of_week", "slot_index"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    therapist_id: Mapped[int] = mapped_column(ForeignKey("therapists.id"), index=True)
    day_of_week: Mapped[int] = mapped_column(Integer)
    slot_index: Mapped[int] = mapped_column(Integer)
    available: Mapped[bool] = mapped_column(Boolean, default=True)


class TherapistAbsence(Base):
    __tablename__ = "therapist_absences"
    id: Mapped[int] = mapped_column(primary_key=True)
    therapist_id: Mapped[int] = mapped_column(ForeignKey("therapists.id"), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    start_slot: Mapped[int | None] = mapped_column(Integer)
    end_slot: Mapped[int | None] = mapped_column(Integer)
    full_day: Mapped[bool] = mapped_column(Boolean, default=True)
    reason: Mapped[str | None] = mapped_column(Text)


class Patient(Base):
    __tablename__ = "patients"
    id: Mapped[int] = mapped_column(primary_key=True)
    patient_code: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(120))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    school_going: Mapped[bool] = mapped_column(Boolean, default=False)
    preferred_time: Mapped[str | None] = mapped_column(String(40))
    notes: Mapped[str | None] = mapped_column(Text)


class PatientAvailability(Base):
    __tablename__ = "patient_availability"
    __table_args__ = (UniqueConstraint("patient_id", "day_of_week", "slot_index"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    day_of_week: Mapped[int] = mapped_column(Integer)
    slot_index: Mapped[int] = mapped_column(Integer)
    available: Mapped[bool] = mapped_column(Boolean, default=True)
    preference_score: Mapped[int] = mapped_column(Integer, default=0)
    patient: Mapped[Patient] = relationship()


class PatientAbsence(Base):
    __tablename__ = "patient_absences"
    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    start_slot: Mapped[int | None] = mapped_column(Integer)
    end_slot: Mapped[int | None] = mapped_column(Integer)
    full_day: Mapped[bool] = mapped_column(Boolean, default=True)
    reason: Mapped[str | None] = mapped_column(Text)


class TreatmentRequirement(Base):
    __tablename__ = "treatment_requirements"
    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id"))
    assigned_therapist_id: Mapped[int] = mapped_column(ForeignKey("therapists.id"))
    location_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id"), nullable=True)
    sessions_per_week: Mapped[int] = mapped_column(Integer)
    sequence_order: Mapped[int] = mapped_column(Integer)
    specialty_name: Mapped[str | None] = mapped_column(String(120))
    duration_months: Mapped[int | None] = mapped_column(Integer)
    plan_start_date: Mapped[date | None] = mapped_column(Date)
    plan_added_at: Mapped[datetime | None] = mapped_column(DateTime)
    source_status: Mapped[str | None] = mapped_column(String(40))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    patient: Mapped[Patient] = relationship()
    department: Mapped[Department] = relationship()
    assigned_therapist: Mapped[Therapist] = relationship()


class Appointment(Base):
    __tablename__ = "appointments"
    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    treatment_requirement_id: Mapped[int] = mapped_column(ForeignKey("treatment_requirements.id"))
    therapist_id: Mapped[int] = mapped_column(ForeignKey("therapists.id"), index=True)
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id"))
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"))
    date: Mapped[date] = mapped_column(Date, index=True)
    slot_index: Mapped[int] = mapped_column(Integer)
    status: Mapped[AppointmentStatus] = mapped_column(Enum(AppointmentStatus))
    bundle_id: Mapped[str | None] = mapped_column(String(80), index=True)
    source: Mapped[str] = mapped_column(String(40), default="solver")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ZohoAppointmentSync(Base):
    """Record the Creator row created for one local appointment.

    This is deliberately a one-way link: the scheduler creates only an
    ``All_Therapy_Schedules`` row in Creator and never writes to a lookup
    or master-data form.
    """

    __tablename__ = "zoho_appointment_syncs"

    id: Mapped[int] = mapped_column(primary_key=True)
    appointment_id: Mapped[int] = mapped_column(
        ForeignKey("appointments.id"), unique=True, index=True
    )
    creator_record_id: Mapped[str | None] = mapped_column(String(80), unique=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AdminBookingDecision(Base):
    """Idempotent local admin decision, including acknowledged warning details."""

    __tablename__ = "admin_booking_decisions"
    request_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    appointment_ids: Mapped[str] = mapped_column(Text)
    warnings: Mapped[str] = mapped_column(Text)
    admin_user: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AdminSlotReservation(Base):
    """Tie each released opening to its explicit confirmed admin reservation."""

    __tablename__ = "admin_slot_reservations"
    source_appointment_id: Mapped[int] = mapped_column(
        ForeignKey("appointments.id"), primary_key=True,
    )
    appointment_id: Mapped[int] = mapped_column(
        ForeignKey("appointments.id"), unique=True,
    )
    decision_id: Mapped[str] = mapped_column(ForeignKey("admin_booking_decisions.request_id"))
    counts_toward_requirement: Mapped[bool] = mapped_column(Boolean, default=True)


class WaitlistEntry(Base):
    __tablename__ = "waitlist_entries"
    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"))
    treatment_requirement_id: Mapped[int] = mapped_column(ForeignKey("treatment_requirements.id"))
    priority: Mapped[int] = mapped_column(Integer, default=0)
    waiting_since: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE")


class ImportWarning(Base):
    __tablename__ = "import_warnings"
    id: Mapped[int] = mapped_column(primary_key=True)
    patient_code: Mapped[str | None] = mapped_column(String(40))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    action: Mapped[str] = mapped_column(String(80))
    entity: Mapped[str] = mapped_column(String(80))
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    user: Mapped[str] = mapped_column(String(80), default="scheduler_manager")


class WhatsAppButtonClick(Base):
    """Retain authenticated reply-button clicks independently of the reply queue."""

    __tablename__ = "whatsapp_button_clicks"
    event_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    message_id: Mapped[str] = mapped_column(String(512))
    phone_number_id: Mapped[str] = mapped_column(String(20))
    user_name: Mapped[str | None] = mapped_column(String(512))
    phone_number: Mapped[str] = mapped_column(String(20), index=True)
    clicked_button: Mapped[str] = mapped_column(String(512))
    button_payload: Mapped[str | None] = mapped_column(Text)
    clicked_at: Mapped[datetime] = mapped_column(KolkataTimestamp(), index=True)
    recorded_at: Mapped[datetime] = mapped_column(KolkataTimestamp(), default=kolkata_now)
    time_zone: Mapped[str] = mapped_column(
        String(64), default=WHATSAPP_TIME_ZONE, server_default=WHATSAPP_TIME_ZONE,
    )
    clicked_at_ist: Mapped[datetime] = mapped_column(
        DateTime, Computed(KolkataWallTime(literal_column("clicked_at")), persisted=True),
    )
    recorded_at_ist: Mapped[datetime] = mapped_column(
        DateTime, Computed(KolkataWallTime(literal_column("recorded_at")), persisted=True),
    )


class WhatsAppHelpReply(Base):
    """Deduplicate Help callbacks and retain the recipient only while replying."""

    __tablename__ = "whatsapp_help_replies"
    event_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    recipient: Mapped[str | None] = mapped_column(String(20))
    received_at: Mapped[datetime] = mapped_column(DateTime)
    state: Mapped[str] = mapped_column(String(20), default="pending", index=True)


class ParentAvailabilityResponse(Base):
    """An assumed parent reply tied to a snapshot of one booked visit.

    Simulation data uses this table directly; no WhatsApp integration is needed.
    A No reply must explicitly identify visit-time or whole-day unavailability.
    """

    __tablename__ = "parent_availability_responses"
    event_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    appointment_id: Mapped[int] = mapped_column(ForeignKey("appointments.id"), index=True)
    appointment_date: Mapped[date] = mapped_column(Date, index=True)
    appointment_slot: Mapped[int] = mapped_column(Integer)
    reply: Mapped[str] = mapped_column(String(3))
    unavailable_scope: Mapped[str | None] = mapped_column(String(10))
    source: Mapped[str] = mapped_column(String(30), default="simulation")
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime)


class PlanDetailSource(Base):
    """Authoritative row-level input copied from the Plan_Detail workbook sheet."""

    __tablename__ = "plan_detail"

    source_row_number: Mapped[int] = mapped_column(primary_key=True)
    registration_id: Mapped[str | None] = mapped_column(String(80), index=True)
    name: Mapped[str | None] = mapped_column(String(120))
    branch: Mapped[str | None] = mapped_column(String(120), index=True)
    therapy_type: Mapped[str | None] = mapped_column(String(120), index=True)
    specialty: Mapped[str | None] = mapped_column(String(120))
    weekly_frequency: Mapped[str | None] = mapped_column(String(40))
    duration_months: Mapped[str | None] = mapped_column(String(40))
    therapist: Mapped[str | None] = mapped_column(String(120), index=True)
    therapist_source: Mapped[str | None] = mapped_column(String(120))
    start_date: Mapped[date | None] = mapped_column(Date)
    plan_added_time: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str | None] = mapped_column(String(80))
    quality_flag: Mapped[str | None] = mapped_column(Text)


class TherapistMasterSource(Base):
    """Authoritative therapist capability and branch input from Therapist_Master."""

    __tablename__ = "therapist_master"

    source_row_number: Mapped[int] = mapped_column(primary_key=True)
    therapist_code: Mapped[str | None] = mapped_column(String(80), index=True)
    therapist_name: Mapped[str | None] = mapped_column(String(120), index=True)
    branches: Mapped[str | None] = mapped_column(Text)
    specialties: Mapped[str | None] = mapped_column(Text)
    therapy_types: Mapped[str | None] = mapped_column(Text)
    source_type: Mapped[str | None] = mapped_column(String(120))
    used_in_current_plans: Mapped[str | None] = mapped_column(String(40))
    current_patients: Mapped[str | None] = mapped_column(String(40))
    current_plan_lines: Mapped[str | None] = mapped_column(String(40))
    historical_source_lines: Mapped[str | None] = mapped_column(String(40))


class SpecialtyMasterSource(Base):
    """Authoritative therapy-to-specialty standardization input from Specialty_Master."""

    __tablename__ = "specialty_master"

    source_row_number: Mapped[int] = mapped_column(primary_key=True)
    therapy_type: Mapped[str | None] = mapped_column(String(120), index=True)
    specialty: Mapped[str | None] = mapped_column(String(120))
    short_code: Mapped[str | None] = mapped_column(String(40))
    typical_capability_area: Mapped[str | None] = mapped_column(Text)


class ZohoPlanDetailRecord(Base):
    """Staged Plan Detail row pulled from the Creator production report."""

    __tablename__ = "zoho_plan_detail_records"

    creator_record_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    registration_id: Mapped[str | None] = mapped_column(String(80), index=True)
    name: Mapped[str | None] = mapped_column(String(120))
    branch: Mapped[str | None] = mapped_column(String(120), index=True)
    therapy_type: Mapped[str | None] = mapped_column(String(120), index=True)
    specialty: Mapped[str | None] = mapped_column(String(120))
    weekly_frequency: Mapped[str | None] = mapped_column(String(40))
    duration_months: Mapped[str | None] = mapped_column(String(40))
    therapist: Mapped[str | None] = mapped_column(String(120), index=True)
    start_date: Mapped[date | None] = mapped_column(Date)
    plan_added_time: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str | None] = mapped_column(String(80))
    pulled_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
