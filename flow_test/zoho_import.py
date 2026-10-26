"""Convert the two approved Zoho test reports without changing the core OR."""

import csv
from datetime import date, datetime, timezone
import hashlib
import json
import re
import time
import unicodedata
from zoneinfo import ZoneInfo

from sqlalchemy import select

from database.models import Appointment, Department, Location, Patient, Therapist, TreatmentRequirement
from database.repository import _plan_end_date
from flow_test.models import FlowInbox, FlowJob, FlowOffer, FlowSetting

OWNER = "nidhin.kumar_turnb"
APP = "test-gokul"
REPORTS = ("Plan_Report", "All_Consultants")
SCENARIO_KEY = "zoho_test_scenario"
CHECKPOINT_KEY = "zoho_test_checkpoint"
SOURCE_TIMEZONES = {"Added_Time": "Asia/Kolkata", "Modified_Time": "Asia/Kolkata"}
TIMEZONE_KEY = "zoho_test_source_timezone"


def clean_text(value) -> str:
    """Normalize pasted whitespace without guessing missing or misspelled data."""
    if value is None:
        return ""
    value = unicodedata.normalize("NFKC", str(value))
    return " ".join(value.translate({ord(c): None for c in "\ufeff\u200b\u2060"}).split())


def branch_key(value) -> str:
    return clean_text(value).removesuffix(" — FICTIONAL TEST").casefold()


def treatment_start(added: datetime) -> date:
    """Added Time is already IST; never add the offset a second time."""
    return added.date()


def sync_now() -> datetime:
    """UTC clock used to reject accidental future checkpoints."""
    return datetime.now(timezone.utc).replace(microsecond=0)


def source_time(value: str) -> datetime:
    """Parse a source wall-clock value; its field determines the time zone."""
    try:
        return datetime.strptime(clean_text(value), "%d-%b-%Y %H:%M:%S")
    except (TypeError, ValueError):
        raise ValueError("Missing or invalid Zoho Added_Time/Modified_Time") from None


def source_instant(value: str, field: str) -> datetime:
    """Compare source fields/checkpoints as instants despite different zones."""
    return source_time(value).replace(tzinfo=ZoneInfo(SOURCE_TIMEZONES[field])).astimezone(timezone.utc)


def rebase_cached_times(reports: dict, previous_zones: dict[str, str] | str | None) -> dict:
    """Translate retained legacy source rows during explicit reconciliation."""
    zones = previous_zones if isinstance(previous_zones, dict) else {
        field: previous_zones or "UTC" for field in SOURCE_TIMEZONES
    }
    result = {}
    for report, rows in reports.items():
        result[report] = []
        for row in rows:
            row = dict(row)
            for field, zone in SOURCE_TIMEZONES.items():
                row[field] = source_time(row[field]).replace(tzinfo=ZoneInfo(zones[field])).astimezone(
                    ZoneInfo(zone)).strftime("%d-%b-%Y %H:%M:%S")
            result[report].append(row)
    return result


def phone_number(value: str) -> str:
    number = re.sub(r"[\s+()-]", "", clean_text(value))
    if not re.fullmatch(r"[1-9]\d{7,14}", number):
        raise ValueError("Use an international parent phone number")
    return number


def checkpoint_time(value: str) -> datetime:
    """Parse a timezone-qualified, second-resolution sync boundary."""
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or result.microsecond:
            raise ValueError
        return result.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError):
        raise ValueError("Use a timezone-qualified sync checkpoint at whole-second precision") from None


def merge_reports(previous: dict | None, incoming: dict, window: dict | None,
                  *, full_snapshot: bool = False, timezone_migration: bool = False) -> dict:
    """Merge by Creator ID; rows absent from a delta are never deleted."""
    if not isinstance(incoming, dict) or set(incoming) != set(REPORTS):
        raise ValueError("Supply both approved test reports")
    since = until = None
    if window is not None:
        if not isinstance(window, dict) or set(window) != {"from", "to"}:
            raise ValueError("Supply the complete sync window")
        since = checkpoint_time(window["from"]) if window["from"] is not None else None
        until = checkpoint_time(window["to"])
        if since is not None and since >= until:
            raise ValueError("Sync window end must be after its checkpoint")
        if until > sync_now():
            raise ValueError("Sync window end cannot be in the future")
    merged = {}
    for report in REPORTS:
        rows = incoming[report]
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("Invalid test report rows")
        cache = {clean_text(row["ID"]): row for row in (previous or {}).get(report, [])}
        seen = set()
        for row in rows:
            source_id = clean_text(row.get("ID"))
            if not source_id.isdigit() or source_id in seen:
                raise ValueError("Missing or duplicate source record ID")
            seen.add(source_id)
            added = source_instant(row.get("Added_Time"), "Added_Time")
            modified = source_instant(row.get("Modified_Time"), "Modified_Time")
            if modified < added:
                raise ValueError("Modified Time cannot precede Added Time")
            if until and (added >= until or modified >= until or
                          (since and not full_snapshot and added < since and modified < since)):
                raise ValueError("Source record lies outside the requested sync window; retry the pull")
            old = cache.get(source_id)
            if old and report == "Plan_Report" and clean_text(old.get("RegistrationID")) != clean_text(row.get("RegistrationID")):
                raise ValueError("Changing an existing source Registration ID requires staff review")
            if old and not timezone_migration and source_time(old["Modified_Time"]) > source_time(row["Modified_Time"]):
                raise ValueError("Source returned an older record version; retry the pull")
            cache[source_id] = row
        merged[report] = list(cache.values())
    return merged


def convert_reports(reports: dict, plan_start: date | None = None) -> dict:
    """Convert source branches and each latest plan's Added Time to OR inputs.

    The optional old plan_start argument is accepted for existing job clients;
    source Added Time now supplies each plan's start date.
    """
    if set(reports) != set(REPORTS):
        raise ValueError("Supply both approved test reports")
    consultants, patients, source_ids, branches, therapy_labels = {}, [], set(), {}, {}
    def branch(value, context):
        name = clean_text(value)
        if not name or len(name) > 100:
            raise ValueError(f"{context}: supply a Branch of at most 100 characters")
        return branches.setdefault(branch_key(name), name)

    for row in reports["All_Consultants"]:
        source_id = clean_text(row.get("ID"))
        name = clean_text(row.get("Name"))
        therapy = clean_text(row.get("ConsultationType"))
        therapy = therapy_labels.setdefault(therapy.casefold(), therapy)
        source_branch = branch(row.get("Branch"), f"Consultant {source_id}")
        key = (branch_key(source_branch), name.casefold(), therapy.casefold())
        status = clean_text(row.get("Status")).casefold()
        if (not source_id.isdigit() or not name or len(name) > 120
                or not therapy or len(therapy) > 120
                or status not in {"active", "inactive"} or key in consultants
                or source_id in source_ids):
            raise ValueError(f"Invalid or ambiguous consultant record {source_id!r} in {source_branch}")
        source_ids.add(source_id)
        source_time(row.get("Added_Time"))
        source_time(row.get("Modified_Time"))
        consultants[key] = {"code": f"ZC-{source_id}", "name": name,
                            "therapy": therapy, "branch": source_branch, "active": status == "active"}
    # Registration ID identifies the patient; edited older rows can be newest.
    versions = {}
    for row in reports["Plan_Report"]:
        code = clean_text(row.get("RegistrationID"))
        version = (source_time(row.get("Modified_Time")), source_time(row.get("Added_Time")))
        versions.setdefault(code, []).append((version, row))
    latest = {}
    for code, candidates in versions.items():
        newest = max(version for version, _ in candidates)
        winners = [row for version, row in candidates if version == newest]
        if len({clean_text(row.get("ID")) for row in winners}) > 1:
            raise ValueError("Ambiguous patient plan versions with identical Modified Time and Added Time")
        latest[code] = winners[0]
    seen = set()
    for row in sorted(latest.values(), key=lambda item: clean_text(item.get("RegistrationID"))):
        code = clean_text(row.get("RegistrationID"))
        source_id = clean_text(row.get("ID"))
        name = clean_text(row.get("PatientName"))
        source_branch = branch(row.get("Branch"), f"Patient {code!r}")
        if (not code or len(code) > 40 or code in seen
                or not source_id.isdigit() or source_id in source_ids
                or not name or len(name) > 120):
            raise ValueError(f"Invalid or duplicate patient record {code!r}")
        seen.add(code)
        source_ids.add(source_id)
        added = source_time(row.get("Added_Time"))
        source_time(row.get("Modified_Time"))
        schedule = row.get("Schedule")
        if not isinstance(schedule, str) or not schedule.strip():
            raise ValueError(f"Missing Schedule for {code}")
        requirements, therapies = [], set()
        for entry in re.split(r"[;\r\n]+", unicodedata.normalize("NFKC", schedule).strip()):
            if not entry.strip():
                continue
            try:
                fields = next(csv.reader([entry], skipinitialspace=True, strict=True))
            except csv.Error:
                raise ValueError(f"Malformed Schedule for {code}") from None
            if len(fields) != 4:
                raise ValueError(f"Schedule for {code} needs therapy, weekly frequency, months, consultant")
            therapy, weekly, months, consultant_name = (clean_text(value) for value in fields)
            consultant = consultants.get((branch_key(source_branch), consultant_name.casefold(), therapy.casefold()))
            if (not weekly.isdecimal() or not months.isdecimal()
                    or int(weekly) < 1 or int(months) < 1
                    or therapy.casefold() in therapies or consultant is None):
                raise ValueError(f"Invalid therapy/frequency/duration or consultant match for {code} in branch {source_branch}")
            therapies.add(therapy.casefold())
            try:
                _plan_end_date(treatment_start(added), int(months))
            except (ValueError, OverflowError):
                raise ValueError(f"Invalid calendar duration for {code}") from None
            requirements.append({"therapy": consultant["therapy"],
                                 "therapist": consultant["code"], "weekly": int(weekly),
                                 "branch": source_branch,
                                 "duration_months": int(months), "plan_start_date": treatment_start(added).isoformat(),
                                 "plan_added_at": added.isoformat()})
        if not requirements:
            raise ValueError(f"Missing Schedule entries for {code}")
        patients.append({"code": code, "name": name, "branch": source_branch,
                         "initial_roster": True, "phone": phone_number(row.get("Parent_No", "")),
                         "requirements": requirements})
    if not patients or not consultants:
        raise ValueError("The initial source needs at least one patient and consultant")
    locations = [{"branch": name, "name": f"{name} — FICTIONAL TEST", "capacity": max(1, sum(
        c["active"] and branch_key(c["branch"]) == key for c in consultants.values()))}
        for key, name in sorted(branches.items())]
    return {"version": "malaparamba-fictional-v3", "source": f"{OWNER}/{APP}",
            "centre": locations[0]["name"], "capacity": locations[0]["capacity"], "locations": locations,
            "therapists": sorted(consultants.values(), key=lambda item: item["code"]),
            "patients": patients}


def put_setting(session, key: str, value) -> None:
    item = session.get(FlowSetting, key)
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if item is None:
        session.add(FlowSetting(key=key, value=serialized))
    else:
        item.value = serialized


def plan_warning(session, warnings: list, code: str, requirement_id: int, text: str) -> None:
    """Persist each distinct plan-change warning without duplicating the inbox."""
    warnings.append({"patient": code, "requirement_id": requirement_id, "message": text})
    key = "plan:" + hashlib.sha256(f"{code}:{requirement_id}:{text}".encode()).hexdigest()
    if session.scalar(select(FlowInbox.id).where(FlowInbox.event_key == key)) is None:
        session.add(FlowInbox(event_key=key, patient_code=code,
                              reason="Zoho plan changed — review saved appointments", text=text))


def apply_reports(flow, reports: dict, plan_start: date | None = None, *, window: dict | None = None,
                  full_snapshot: bool = False) -> dict:
    """Commit merged source records, projections and checkpoint in one transaction."""
    with flow.lock, flow.inbound_lock:
        return _apply_reports_locked(flow, reports, plan_start, window=window, full_snapshot=full_snapshot)


def _apply_reports_locked(flow, reports: dict, plan_start: date | None, *, window: dict | None,
                          full_snapshot: bool) -> dict:
    with flow.lock, flow.inbound_lock, flow.sessions.begin() as session:
        if window is not None and not isinstance(window, dict):
            raise ValueError("Supply the complete sync window")
        checkpoint = session.get(FlowSetting, CHECKPOINT_KEY)
        current = json.loads(checkpoint.value) if checkpoint else None
        source_zone = session.get(FlowSetting, TIMEZONE_KEY)
        if type(full_snapshot) is not bool or (full_snapshot and window is None):
            raise ValueError("Full reconciliation requires a bounded sync window")
        cache = session.get(FlowSetting, "zoho_test_source")
        previous_zones = json.loads(source_zone.value) if source_zone else None
        if (current is not None or cache is not None) and previous_zones != SOURCE_TIMEZONES and not full_snapshot:
            raise ValueError("Source timestamp timezone changed; run --reconcile once before incremental sync")
        if window is not None and window.get("from") != current:
            raise ValueError("Sync checkpoint changed; fetch status and retry without advancing it")
        if window is None and current is not None:
            raise ValueError("Use a checkpoint window for this incremental source")
        previous_reports = json.loads(cache.value) if cache else None
        timezone_migration = previous_zones != SOURCE_TIMEZONES
        if previous_reports and timezone_migration:
            previous_reports = rebase_cached_times(previous_reports, previous_zones)
        reports = merge_reports(previous_reports, reports, window,
                                full_snapshot=full_snapshot,
                                timezone_migration=bool(full_snapshot and timezone_migration))
        scenario = convert_reports(reports, plan_start)
        digest = hashlib.sha256(json.dumps(scenario, sort_keys=True).encode()).hexdigest()
        marker = session.get(FlowSetting, "scenario")
        if marker is None or marker.value != "malaparamba-fictional-v3":
            raise ValueError("Import requires the known v3 test workspace")
        previous = session.get(FlowSetting, SCENARIO_KEY)
        if previous is None and session.scalar(select(Appointment.id).limit(1)) is not None:
            raise ValueError("Existing fixture appointments prevent switching to Zoho. Start a deliberate fresh test first.")
        old_hash = session.get(FlowSetting, "zoho_test_fingerprint")
        changed = old_hash is None or json.loads(old_hash.value) != digest
        locations = {branch_key(item.name): item for item in session.scalars(select(Location))}
        for row in scenario["locations"]:
            key = branch_key(row["branch"])
            location = locations.get(key)
            if location is None:
                location = Location(name=row["name"], capacity=row["capacity"], active=True)
                session.add(location)
                session.flush()
                locations[key] = location
            location.capacity, location.active = row["capacity"], True
        departments = {clean_text(item.name).casefold(): item for item in session.scalars(select(Department))}
        therapists = {item.therapist_code: item for item in session.scalars(select(Therapist))}
        source_codes = {item["code"] for item in scenario["therapists"]}
        for row in scenario["therapists"]:
            location = locations[branch_key(row["branch"])]
            therapy_key = row["therapy"].casefold()
            if therapy_key not in departments:
                department = Department(name=row["therapy"], active=True)
                session.add(department)
                session.flush()
                departments[therapy_key] = department
            therapist = therapists.get(row["code"])
            if therapist is None:
                therapist = Therapist(therapist_code=row["code"], name=row["name"],
                                      department_id=departments[therapy_key].id,
                                      location_id=location.id, active=row["active"])
                session.add(therapist)
                session.flush()
                therapists[row["code"]] = therapist
            elif (therapist.department_id != departments[therapy_key].id
                  or therapist.location_id != location.id):
                raise ValueError("Consultant capability changes require staff review")
            if therapist.active != row["active"] and session.scalar(select(Appointment.id).where(
                    Appointment.therapist_id == therapist.id).limit(1)) is not None:
                raise ValueError("Consultant status change affects saved appointments; staff review required")
            therapist.name, therapist.active = row["name"], row["active"]
        for code, therapist in therapists.items():
            if code not in source_codes:
                therapist.active = False
        warnings = []
        by_code = {item.patient_code: item for item in session.scalars(select(Patient))}
        if previous is None:
            source_patient_codes = {item["code"] for item in scenario["patients"]}
            for code, item in by_code.items():
                if code not in source_patient_codes:
                    item.active = False
                    for requirement in session.scalars(select(TreatmentRequirement).where(
                            TreatmentRequirement.patient_id == item.id)):
                        requirement.active = False
        for row in scenario["patients"]:
            patient = by_code.get(row["code"])
            if patient is None:
                patient = Patient(patient_code=row["code"], name=row["name"], active=True)
                session.add(patient)
                session.flush()
            patient.name, patient.active = row["name"], True
            active = list(session.scalars(select(TreatmentRequirement).where(
                TreatmentRequirement.patient_id == patient.id, TreatmentRequirement.active.is_(True))))
            retained = set()
            for index, requirement in enumerate(row["requirements"]):
                department = departments[requirement["therapy"].casefold()]
                location = locations[branch_key(requirement["branch"])]
                existing = next((item for item in active if item.department_id == department.id), None)
                values = {"department_id": department.id,
                          "assigned_therapist_id": therapists[requirement["therapist"]].id,
                          "location_id": location.id, "sessions_per_week": requirement["weekly"],
                          "sequence_order": index, "duration_months": requirement["duration_months"],
                          "plan_start_date": date.fromisoformat(requirement["plan_start_date"])}
                if existing is None:
                    existing = TreatmentRequirement(patient_id=patient.id, active=True, **values)
                    session.add(existing)
                    session.flush()
                elif any(getattr(existing, key) != value for key, value in values.items()):
                    if session.scalar(select(Appointment.id).where(
                            Appointment.treatment_requirement_id == existing.id).limit(1)) is not None:
                        structural = any(getattr(existing, key) != values[key] for key in (
                            "assigned_therapist_id", "location_id"))
                        if structural:
                            plan_warning(session, warnings, row["code"], existing.id,
                                         f"{requirement['therapy']}: new doctor or branch needs staff review. "
                                         f"Requested: {therapists[requirement['therapist']].name} at {requirement['branch']}. "
                                         "The existing requirement and bookings are retained; other records were imported.")
                            retained.add(existing.id)
                            continue
                        labels = {"sessions_per_week": "Sessions per week", "plan_start_date": "Treatment start",
                                  "duration_months": "Duration in months", "sequence_order": "Therapy order"}
                        changes = "; ".join(f"{labels.get(key, key)}: {getattr(existing, key)} → {value}"
                                            for key, value in values.items() if getattr(existing, key) != value)
                        plan_warning(session, warnings, row["code"], existing.id,
                                     f"{requirement['therapy']}: {changes}. Latest target and dates applied; "
                                     "saved bookings and completed sessions are unchanged. Review any excess or out-of-plan bookings.")
                    for key, value in values.items():
                        setattr(existing, key, value)
                    existing.source_status = None
                existing.plan_added_at = datetime.fromisoformat(requirement["plan_added_at"])
                retained.add(existing.id)
            for item in active:
                if item.id not in retained:
                    if session.scalar(select(Appointment.id).where(
                            Appointment.treatment_requirement_id == item.id).limit(1)) is not None:
                        if item.sessions_per_week != 0:
                            plan_warning(session, warnings, row["code"], item.id,
                                         "Therapy removed from latest plan. Future demand is now zero; "
                                         "existing bookings and completed sessions are retained for staff review.")
                        # Keep the historical assignment available to validate
                        # pinned bookings, but generate no new demand for it.
                        item.sessions_per_week = 0
                        item.source_status = "removed_from_source"
                    else:
                        item.active = False
        # Rebind only offers for which no provider send has been attempted.
        # Sent or uncertain offers remain tied to their original recipient.
        phones = {item["code"]: item["phone"] for item in scenario["patients"]}
        for offer in session.scalars(select(FlowOffer).where(FlowOffer.state == "PENDING")):
            number = phones.get(offer.patient_code)
            if not number or number == offer.recipient:
                continue
            key = hashlib.sha256(f"offer:{offer.token}".encode()).hexdigest()
            job = session.scalar(select(FlowJob).where(FlowJob.event_key == key))
            if not job or job.kind != "OFFER" or job.status not in {"QUEUED", "WAITING_FOR_CONTACT"} or job.message_id:
                continue
            payload = json.loads(job.payload)
            payload["recipient"] = number
            payload.pop("session_expires_at", None)
            job.payload = json.dumps(payload, ensure_ascii=False)
            offer.recipient = number
            if flow.live and flow.message_mode == "session" and flow._session_deadline(session, number) <= time.time():
                job.status = "WAITING_FOR_CONTACT"
                job.error = "Parent must message first; no send was attempted"
            else:
                job.status, job.error = "QUEUED", None
            flow._audit(session, "flow_offer_recipient_updated", f"offer:{offer.token}")
        put_setting(session, SCENARIO_KEY, scenario)
        put_setting(session, "zoho_test_source", reports)
        put_setting(session, "zoho_test_fingerprint", digest)
        put_setting(session, TIMEZONE_KEY, SOURCE_TIMEZONES)
        if window is not None:
            put_setting(session, CHECKPOINT_KEY, window["to"])
        result = {"patients": len(scenario["patients"]), "consultants": len(scenario["therapists"]), "requirements": sum(
            len(item["requirements"]) for item in scenario["patients"]),
                  "weekly_sessions": sum(req["weekly"] for item in scenario["patients"] for req in item["requirements"]),
                  "changed": changed, "plan_start_source": "Added_Time (Asia/Kolkata date)",
                  "branches": len(scenario["locations"]),
                  "warnings": warnings,
                  "synced_at": datetime.utcnow().isoformat() + "Z",
                  "checkpoint": window["to"] if window else current}
        put_setting(session, "zoho_test_sync", result)
        if changed:
            flow._audit(session, "flow_zoho_imported", "test-gokul")
    flow.use_source_contacts(scenario)
    return result
