"""Durable manual schedule finalisation/export, isolated from the source importer."""

from datetime import date, datetime, timezone
import hashlib
import json
from typing import TYPE_CHECKING, Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from database.models import Appointment, AppointmentStatus, AuditLog, Patient, Therapist
from flow_test.models import FlowInbox, FlowJob, FlowOffer, FlowSetting
from flow_test.zoho_import import put_setting
from scheduling.slot_config import SLOTS

if TYPE_CHECKING:
    from flow_test.service import FlowTest


TARGET = "nidhin.kumar_turnb/test-gokul/Scheduler_Report"
PREFIX = "zoho_scheduler_export"
FINAL = {AppointmentStatus.CONFIRMED, AppointmentStatus.COMPLETED, AppointmentStatus.NO_SHOW}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ist_time(value: datetime) -> str:
    return value.astimezone(ZoneInfo("Asia/Kolkata")).isoformat(timespec="seconds")


def setting(session: Session, key: str, default: Any = None) -> Any:
    item = session.get(FlowSetting, f"{PREFIX}:{key}")
    return json.loads(item.value) if item else default


def store(session: Session, key: str, value: Any) -> None:
    put_setting(session, f"{PREFIX}:{key}", value)


def finalise_unanswered(flow: "FlowTest", session: Session, target: date, *, dry_run: bool = False) -> int:
    """Manual cutoff assumes acceptance only for sent visits with no parent reply."""
    assumed = 0
    inbound = list(session.scalars(select(FlowJob).where(FlowJob.kind == "INBOUND")))
    unresolved = list(session.scalars(select(FlowInbox).where(FlowInbox.resolved == 0)))
    for offer in session.scalars(select(FlowOffer).where(FlowOffer.state.in_(("PENDING", "EXPIRED")))):
        try:
            items = flow._offer_items(session, offer)
        except ValueError:
            continue  # Changed offers require staff review, never implicit acceptance.
        if not items or any(a.date != target for a in items):
            continue
        sent = session.scalar(select(FlowJob).where(
            FlowJob.event_key == hashlib.sha256(f"offer:{offer.token}".encode()).hexdigest(),
            FlowJob.status.in_(("SENT",) if flow.live else ("SENT", "SIMULATED")),
            (FlowJob.delivery.is_(None) | (FlowJob.delivery != "failed"))))
        replied = session.scalar(select(AuditLog.id).where(
            AuditLog.entity == f"offer:{offer.token}", AuditLog.action.like("flow_reply_%")))
        # A late/invalid response is still a response. Resolving its inbox item
        # alone is not family acceptance; staff must confirm the booking explicitly.
        referenced_reply = any(
            json.loads(job.payload).get("from") == offer.recipient and (
                offer.token in job.payload or
                (sent is not None and sent.message_id is not None and
                 json.loads(job.payload).get("context", {}).get("id") == sent.message_id))
            for job in inbound)
        review = any(item.patient_code == offer.patient_code and not item.event_key.startswith("expiry:")
                     for item in unresolved)
        ambiguous = any(item.event_key == job.event_key and item.patient_code is None
                        and job.created_at >= offer.created_at
                        and json.loads(job.payload).get("from") == offer.recipient
                        for item in unresolved for job in inbound)
        if sent is None or replied is not None or referenced_reply or review or ambiguous or any(
                a.status != AppointmentStatus.PENDING_CONFIRMATION for a in items):
            continue
        if dry_run:
            assumed += 1
            continue
        for item in items:
            item.status = AppointmentStatus.CONFIRMED
        offer.state, offer.expires_at = "CONFIRMED", None
        expiry = session.scalar(select(FlowInbox).where(FlowInbox.event_key == f"expiry:{offer.token}"))
        if expiry:
            expiry.resolved = 1
        flow._audit(session, "flow_assumed_yes", f"offer:{offer.token}")
        assumed += 1
    session.flush()
    return assumed


def export_rows(flow: "FlowTest", session: Session, target: date, namespace: str) -> tuple[list[dict], list[int]]:
    """Snapshot saved appointments, including withdrawals of previously exported rows."""
    assumed_ids = set()
    for offer in session.scalars(select(FlowOffer)):
        assumed = session.scalar(select(AuditLog.id).where(
            AuditLog.entity == f"offer:{offer.token}", AuditLog.action == "flow_assumed_yes"))
        yes = session.scalar(select(AuditLog.id).where(
            AuditLog.entity == f"offer:{offer.token}", AuditLog.action == "flow_reply_yes"))
        if assumed is not None and yes is None:
            assumed_ids.update(json.loads(offer.appointment_ids))
    rows, blocked = [], []
    tracked_ids = [int(item.key.rsplit(":", 1)[-1]) for item in session.scalars(select(FlowSetting).where(
        FlowSetting.key.like(f"{PREFIX}:item:%")))
        if json.loads(item.value).get("therapyDate") == target.strftime("%d-%m-%Y")]
    for item, patient, doctor in session.execute(select(Appointment, Patient, Therapist).join(
            Patient, Patient.id == Appointment.patient_id).join(
            Therapist, Therapist.id == Appointment.therapist_id).where(
                (Appointment.date == target) | Appointment.id.in_(tracked_ids)).order_by(Appointment.id)):
        previous = setting(session, f"item:{item.id}")
        if item.status not in FINAL and previous is None:
            if item.status in {AppointmentStatus.PROPOSED, AppointmentStatus.PENDING_CONFIRMATION}:
                blocked.append(item.id)
            continue
        status = item.status.value
        if status == "CONFIRMED" and item.id in assumed_ids:
            status = "ASSUMED_CONFIRMED"
        fields = {"Scheduler_Key": f"{namespace}:{item.id}", "Reg_ID": patient.patient_code,
                  "PatientName": patient.name or patient.patient_code, "Doctor": doctor.name,
                  "Therapy_Date": item.date.strftime("%d-%m-%Y"),
                  "TherapyTime": SLOTS[item.slot_index].start_time.strftime("%I:%M %p"), "Status": status}
        digest = hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()
        if previous and previous["fingerprint"] == digest:
            continue
        rows.append({"appointmentId": item.id, "fingerprint": digest, "fields": fields})
    return rows, blocked


def export_preview(flow: "FlowTest", target: date) -> dict:
    with flow.lock, flow.sessions() as session:
        namespace = setting(session, "namespace", "preview")
        rows, blocked = export_rows(flow, session, target, namespace)
        return {"target": TARGET, "date": target.isoformat(), "changes": rows,
                "awaitingFinalisation": blocked, "lastPush": setting(session, "last_push"),
                "wouldAssumeAcceptedVisits": finalise_unanswered(flow, session, target, dry_run=True),
                "openBatch": setting(session, "batch"), "mode": "PREVIEW ONLY"}


def prepare_export(flow: "FlowTest", target: date) -> dict:
    """Freeze one resumable batch; parent events already queued take precedence."""
    with flow.lock, flow.inbound_lock:
        with flow.sessions() as session:
            batch = setting(session, "batch")
        if batch is not None:
            if batch["date"] != target.isoformat():
                raise ValueError("Finish the existing export batch before exporting another date")
            return {**batch, "resumed": True}
        # Do not assume Yes while an already-received No/Help is waiting to be handled.
        with flow.sessions() as session:
            pending = list(session.scalars(select(FlowJob.id).where(
                FlowJob.kind == "INBOUND", FlowJob.status == "QUEUED").order_by(FlowJob.id)))
        for job_id in pending:
            flow.process_one(job_id=job_id)
        with flow.sessions.begin() as session:
            if session.scalar(select(FlowJob.id).where(
                    FlowJob.kind == "INBOUND", FlowJob.status.in_(("FAILED", "PROCESSING")))) is not None:
                raise ValueError("An incoming reply needs staff review before finalising the schedule")
            namespace = setting(session, "namespace") or f"test-{uuid4().hex}"
            store(session, "namespace", namespace)
            assumed = finalise_unanswered(flow, session, target)
            rows, blocked = export_rows(flow, session, target, namespace)
            batch = {"id": uuid4().hex, "date": target.isoformat(), "target": TARGET,
                     "cutoffIST": ist_time(utc_now()), "assumedAcceptedVisits": assumed,
                     "changes": rows, "awaitingStaff": blocked, "acknowledged": {}}
            store(session, "batch", batch)
            flow._audit(session, "flow_zoho_export_prepared", f"export:{batch['id']}")
            return batch


def acknowledge_export(flow: "FlowTest", batch_id: str, appointment_id: int, fingerprint: str, record_id: str) -> dict:
    if not isinstance(record_id, str) or not record_id.isdigit():
        raise ValueError("Zoho must return a numeric record ID")
    with flow.lock, flow.sessions.begin() as session:
        batch = setting(session, "batch")
        row = next((r for r in (batch or {}).get("changes", []) if r["appointmentId"] == appointment_id), None)
        if not batch or batch["id"] != batch_id or not row or row["fingerprint"] != fingerprint:
            raise ValueError("Export acknowledgement does not match the prepared batch")
        batch["acknowledged"][str(appointment_id)] = record_id
        store(session, "batch", batch)
        store(session, f"item:{appointment_id}", {"fingerprint": fingerprint, "recordId": record_id,
                                                "therapyDate": row["fields"]["Therapy_Date"]})
        return {"acknowledged": appointment_id}


def finish_export(flow: "FlowTest", batch_id: str) -> dict:
    with flow.lock, flow.sessions.begin() as session:
        batch = setting(session, "batch")
        if not batch or batch["id"] != batch_id:
            raise ValueError("Export batch is no longer current")
        if any(str(r["appointmentId"]) not in batch["acknowledged"] for r in batch["changes"]):
            raise ValueError("Export is incomplete; last successful push time was not advanced")
        result = {"batchId": batch_id, "date": batch["date"], "capturedThroughIST": batch["cutoffIST"],
                  "completedAtIST": ist_time(utc_now()), "pushed": len(batch["changes"]),
                  "awaitingStaff": batch["awaitingStaff"]}
        store(session, "last_push", result)
        session.delete(session.get(FlowSetting, f"{PREFIX}:batch"))
        flow._audit(session, "flow_zoho_export_completed", f"export:{batch_id}")
        return result


def dispatch_export(request, instance, operation: str) -> None:
    import web_app

    body = request.read_json()
    try:
        if operation in {"preview", "prepare"}:
            target = web_app.parse_date(body.get("date"), "date")
            result = (export_preview if operation == "preview" else prepare_export)(instance.flow, target)
            if operation == "prepare":
                instance.previews.clear()
        elif operation == "ack":
            if type(body.get("appointmentId")) is not int:
                raise ValueError("Supply the exported appointment ID")
            result = acknowledge_export(instance.flow, body.get("batchId"), body["appointmentId"],
                                        body.get("fingerprint"), body.get("recordId"))
        else:
            result = finish_export(instance.flow, body.get("batchId"))
        request.send_json(result)
    except ValueError as exc:
        raise web_app.ApiError(409, str(exc)) from None
