from __future__ import annotations

from collections import Counter
from datetime import timedelta

from sqlalchemy import select

from database.models import (
    Appointment,
    AppointmentStatus,
    Patient,
    PatientAbsence,
    PatientAvailability,
    TherapistAbsence,
    TherapistAvailability,
    TreatmentRequirement,
    WaitlistEntry,
)


ACTIVE_REPLACEMENT_STATUSES = (
    AppointmentStatus.PROPOSED,
    AppointmentStatus.PENDING_CONFIRMATION,
    AppointmentStatus.CONFIRMED,
)


def find_replacement_candidates(session, vacant: Appointment, week_start):
    """Return children who can use an opening without breaking continuity.

    A candidate must be assigned to the same therapist and need the same
    therapy at the same branch. The list is advisory; it never books a child
    automatically.
    """
    day = vacant.date.weekday()
    requirements = list(session.scalars(select(TreatmentRequirement).where(
        TreatmentRequirement.department_id == vacant.department_id,
        TreatmentRequirement.assigned_therapist_id == vacant.therapist_id,
        TreatmentRequirement.location_id == vacant.location_id,
        TreatmentRequirement.active.is_(True),
        TreatmentRequirement.patient_id != vacant.patient_id,
    )))
    week_end = week_start + timedelta(days=6)
    progress = Counter(session.scalars(select(Appointment.treatment_requirement_id).where(
        Appointment.date >= week_start,
        Appointment.date <= week_end,
        Appointment.status.in_([
            AppointmentStatus.CONFIRMED,
            AppointmentStatus.COMPLETED,
            AppointmentStatus.PROPOSED,
        ]),
    )))
    candidates = []
    for requirement in requirements:
        if progress[requirement.id] >= requirement.sessions_per_week:
            continue
        available = session.scalar(select(PatientAvailability.id).where(
            PatientAvailability.patient_id == requirement.patient_id,
            PatientAvailability.day_of_week == day,
            PatientAvailability.slot_index == vacant.slot_index,
            PatientAvailability.available.is_(True),
        ))
        clash = session.scalar(select(Appointment.id).where(
            Appointment.patient_id == requirement.patient_id,
            Appointment.date == vacant.date,
            Appointment.status.notin_([AppointmentStatus.CANCELLED_PATIENT, AppointmentStatus.CANCELLED_OTHER]),
        ))
        patient_absence = session.scalar(select(PatientAbsence.id).where(
            PatientAbsence.patient_id == requirement.patient_id,
            PatientAbsence.date == vacant.date,
        ))
        if not available or clash or patient_absence:
            continue
        patient = session.get(Patient, requirement.patient_id)
        waitlist = session.scalar(select(WaitlistEntry).where(
            WaitlistEntry.treatment_requirement_id == requirement.id,
            WaitlistEntry.status == "ACTIVE",
        ))
        priority = waitlist.priority if waitlist else 0
        waiting_days = (vacant.date - waitlist.waiting_since).days if waitlist else 0
        score = 100_000 + (10_000 if waitlist else 0) + priority * 100 + max(0, waiting_days)
        candidates.append({
            "patient_id": patient.id,
            "patient_code": patient.patient_code,
            "patient_name": patient.name or patient.patient_code,
            "requirement_id": requirement.id,
            "weekly_progress": f"{progress[requirement.id]}/{requirement.sessions_per_week}",
            "reason": (
                "Needs this therapy with the same therapist at the same branch "
                f"and is available at slot {vacant.slot_index}."
            ),
            "priority": priority,
            "score": score,
        })
    return sorted(candidates, key=lambda item: (-item["score"], item["patient_code"]))


def find_pull_forward_candidates(session, vacant: Appointment, horizon_end):
    """Find later continuous visits that could start in a cancelled opening."""
    future = list(session.scalars(select(Appointment).where(
        Appointment.date > vacant.date,
        Appointment.date <= horizon_end,
        Appointment.therapist_id == vacant.therapist_id,
        Appointment.department_id == vacant.department_id,
        Appointment.status.in_(ACTIVE_REPLACEMENT_STATUSES),
    ).order_by(Appointment.date, Appointment.slot_index)))
    candidates = []
    seen_bundles = set()
    for appointment in future:
        bundle_key = (
            appointment.patient_id,
            appointment.date,
            appointment.bundle_id or f"A-{appointment.id}",
        )
        if bundle_key in seen_bundles:
            continue
        seen_bundles.add(bundle_key)
        group = [appointment]
        if appointment.bundle_id:
            group = list(session.scalars(select(Appointment).where(
                Appointment.patient_id == appointment.patient_id,
                Appointment.date == appointment.date,
                Appointment.bundle_id == appointment.bundle_id,
                Appointment.status.in_(ACTIVE_REPLACEMENT_STATUSES),
            ).order_by(Appointment.slot_index)))
        if appointment.id != group[0].id:
            continue
        if vacant.slot_index + len(group) > 10:
            continue

        conflicting_visit = session.scalar(select(Appointment.id).where(
            Appointment.patient_id == appointment.patient_id,
            Appointment.date == vacant.date,
            Appointment.status.in_(ACTIVE_REPLACEMENT_STATUSES),
        ))
        patient_absence = session.scalar(select(PatientAbsence.id).where(
            PatientAbsence.patient_id == appointment.patient_id,
            PatientAbsence.date == vacant.date,
        ))
        if conflicting_visit or patient_absence:
            continue

        target_weekday = vacant.date.weekday()
        viable = True
        for offset, item in enumerate(group):
            target_slot = vacant.slot_index + offset
            patient_available = session.scalar(select(PatientAvailability.id).where(
                PatientAvailability.patient_id == item.patient_id,
                PatientAvailability.day_of_week == target_weekday,
                PatientAvailability.slot_index == target_slot,
                PatientAvailability.available.is_(True),
            ))
            therapist_available = session.scalar(select(TherapistAvailability.id).where(
                TherapistAvailability.therapist_id == item.therapist_id,
                TherapistAvailability.day_of_week == target_weekday,
                TherapistAvailability.slot_index == target_slot,
                TherapistAvailability.available.is_(True),
            ))
            therapist_absent = session.scalar(select(TherapistAbsence.id).where(
                TherapistAbsence.therapist_id == item.therapist_id,
                TherapistAbsence.date == vacant.date,
            ))
            if not patient_available or not therapist_available or therapist_absent:
                viable = False
                break
        if not viable:
            continue

        patient = session.get(Patient, appointment.patient_id)
        days_later = (appointment.date - vacant.date).days
        candidates.append({
            "appointment_id": appointment.id,
            "patient_code": patient.patient_code,
            "scheduled_date": appointment.date,
            "days_later": days_later,
            "current_status": appointment.status.value,
            "continuous_appointments": len(group),
            "reason": (
                f"Scheduled {days_later} day(s) later and available for the cancelled opening."
            ),
            "score": 1_000_000 - days_later * 10_000,
        })
    return sorted(candidates, key=lambda item: (-item["score"], item["patient_code"]))
