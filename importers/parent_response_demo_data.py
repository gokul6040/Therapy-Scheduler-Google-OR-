"""Deterministic, wholly fictional 80-patient response-demo fixture."""

from datetime import date, timedelta
import json

from importers.parent_response_demo_importer import LARGE_FIXTURE


def build_large_demo() -> dict:
    """Build valid original visits, varied weekly demand, and explicit No replies."""
    target = date(2026, 10, 19)
    therapies = ["Speech", "Occupational Therapy", "Physiotherapy"]
    data = {
        "target_date": target.isoformat(),
        "description": "Fictional 80-patient simulation; No means whole-day unavailability in this fixture.",
        "departments": therapies,
        "locations": [{"name": name, "capacity": 6} for name in ("Demo North", "Demo South")],
        "therapists": [], "patients": [], "appointments": [],
        "responses": [], "patient_unavailable": [],
    }

    def patient(team: int, paired: bool, frequency: int, note: str, active=True, future=False):
        code = f"DEMO-{len(data['patients']) + 1:03d}"
        rows = []
        for therapy_index in ([0, 1] if paired else [2]):
            rows.append({
                "therapy": therapies[therapy_index],
                "therapist": f"DEMO-T{team * 3 + therapy_index + 1:02d}",
                "sessions_per_week": frequency,
                "plan_start_date": (target + timedelta(days=7) if future else target).isoformat(),
                "duration_months": 6,
            })
        item = {
            "code": code, "name": f"Demo Child {len(data['patients']) + 1:03d}",
            "active": active, "notes": note, "requirements": rows,
        }
        data["patients"].append(item)
        return item

    def visit(item: dict, day: int, slot: int, status: str) -> str:
        bundle = f"{item['code']}-D{day}-S{slot}"
        first = ""
        for offset, row in enumerate(item["requirements"]):
            key = f"{bundle}-{offset}"
            first = first or key
            data["appointments"].append({
                "key": key, "bundle": bundle, "patient": item["code"],
                "therapy": row["therapy"], "day_offset": day,
                "slot": slot + offset, "status": status,
            })
        return first

    for team in range(4):
        branch = "Demo North" if team < 2 else "Demo South"
        for index, therapy in enumerate(therapies):
            data["therapists"].append({
                "code": f"DEMO-T{team * 3 + index + 1:02d}",
                "name": f"Demo Therapist {team * 3 + index + 1:02d}",
                "therapy": therapy, "branch": branch,
            })
        replies = ["YES", "NO", None, "YES", "NO", None, "NO", "YES"]
        for index, reply in enumerate(replies):
            item = patient(team, index < 4, 2 + index % 2, "Original Monday visit")
            anchor = visit(item, 0, [0, 2, 5, 7][index % 4], "PENDING_CONFIRMATION")
            if reply:
                response = {
                    "event_id": f"large-{item['code']}",
                    "appointment": anchor, "reply": reply,
                }
                if reply == "NO":
                    response["unavailable_scope"] = "day"
                data["responses"].append(response)
        for paired in (True, False):
            for index in range(6):
                notes = [
                    "Weekly target already booked", "One existing Tuesday visit",
                    "One existing Wednesday visit", "Plan starts next week",
                    "Inactive patient", "Available replacement candidate",
                ]
                item = patient(
                    team, paired, 3 if index == 2 else 2, notes[index],
                    active=index != 4, future=index == 3,
                )
                if index == 0:
                    visit(item, 1, 0, "CONFIRMED")
                    visit(item, 3, 0, "CONFIRMED")
                elif index == 1:
                    visit(item, 1, 2, "CONFIRMED")
                elif index == 2:
                    visit(item, 2, 5, "CONFIRMED")
                if team == 3 and not paired:
                    data["patient_unavailable"].append({
                        "patient": item["code"], "weekday": 0, "slot": 5,
                    })
    return data


if __name__ == "__main__":
    LARGE_FIXTURE.write_text(json.dumps(build_large_demo(), indent=2) + "\n", encoding="utf-8")
    print(f"Wrote fictional fixture: {LARGE_FIXTURE}")
