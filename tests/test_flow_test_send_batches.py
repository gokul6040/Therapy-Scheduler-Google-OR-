"""Parallel outbound sends retain per-number order, durable results and reset safety."""

from datetime import date, datetime, timedelta
from threading import Barrier, Event, Thread

from sqlalchemy import select

from flow_test.models import FlowJob, FlowOffer
from flow_test.service import FlowTest


DAY = date(2026, 10, 19)


def test_distinct_recipients_send_concurrently_and_failures_are_not_retried(tmp_path, legacy_flow_scenario, monkeypatch):
    monkeypatch.setenv("FLOW_TEST_SEND_BATCH_SIZE", "3")
    barrier = Barrier(3)
    sent = []
    def send(kind, payload):
        barrier.wait(timeout=5)
        sent.append(payload["recipient"])
        if payload["recipient"] == "preview:TEST-B":
            raise RuntimeError("Simulated ambiguous send")
        return "dummy-" + payload["token"]
    flow = FlowTest(f"sqlite:///{(tmp_path / 'parallel.sqlite').as_posix()}",
                    scenario=legacy_flow_scenario, send=send)
    try:
        flow.generate(DAY)
        flow.publish(DAY)
        assert flow.process_batch() == 3
        assert len(sent) == 3
        with flow.sessions() as session:
            assert sorted(session.scalars(select(FlowJob.status))) == ["SIMULATED", "SIMULATED", "UNKNOWN"]
        assert flow.process_batch() == 0
        assert len(sent) == 3
    finally:
        flow.engine.dispose()


def test_shared_number_is_sent_once_per_batch_in_order(tmp_path, legacy_flow_scenario):
    sent = []
    flow = FlowTest(f"sqlite:///{(tmp_path / 'ordered.sqlite').as_posix()}",
                    scenario=legacy_flow_scenario,
                    recipients={code: "919999999999" for code in ("TEST-A", "TEST-B", "TEST-C")},
                    send=lambda _, payload: sent.append(payload["token"]) or "dummy")
    try:
        flow.generate(DAY)
        flow.publish(DAY)
        with flow.sessions() as session:
            order = [o.token for o in session.scalars(select(FlowOffer).order_by(FlowOffer.created_at))]
        assert flow.process_batch() == 1
        assert flow.process_batch() == 1
        assert flow.process_batch() == 1
        assert sent == order
    finally:
        flow.engine.dispose()


def test_live_shared_number_cooldown_does_not_hold_other_parent(tmp_path, legacy_flow_scenario, monkeypatch):
    sent = []
    flow = FlowTest(f"sqlite:///{(tmp_path / 'cooldown.sqlite').as_posix()}",
                    scenario=legacy_flow_scenario,
                    recipients={"TEST-A": "919999999991", "TEST-B": "919999999991", "TEST-C": "919999999992"},
                    send=lambda _, payload: sent.append(payload["recipient"]) or "dummy")
    try:
        flow.generate(DAY)
        flow.publish(DAY)
        flow.live = True
        flow.message_mode = "template"
        monkeypatch.setattr("flow_test.service.datetime", type("Clock", (), {
            "now": staticmethod(lambda *args: datetime(2026, 10, 19)),
            "utcnow": staticmethod(lambda: datetime(2026, 10, 19)),
        }))
        now = [100.0]
        monkeypatch.setattr("flow_test.service.time.monotonic", lambda: now[0])
        assert flow.process_batch() == 2
        assert flow.process_batch() == 0
        now[0] = 106.0
        assert flow.process_batch() == 1
        assert sent.count("919999999991") == 2 and sent.count("919999999992") == 1
    finally:
        flow.engine.dispose()


def test_reset_waits_for_inflight_batch(tmp_path, legacy_flow_scenario):
    entered, release, reset_done = Event(), Event(), Event()
    def send(*_):
        entered.set()
        assert release.wait(timeout=5)
        return "dummy"
    flow = FlowTest(f"sqlite:///{(tmp_path / 'reset.sqlite').as_posix()}",
                    scenario=legacy_flow_scenario, send=send)
    flow.generate(DAY)
    flow.publish(DAY)
    worker = Thread(target=flow.process_batch)
    reset = Thread(target=lambda: (flow.reset_test_data("RESET preview"), reset_done.set()))
    try:
        worker.start()
        assert entered.wait(timeout=5)
        reset.start()
        assert not reset_done.wait(timeout=0.1)
        release.set()
        worker.join(timeout=5)
        reset.join(timeout=5)
        assert reset_done.is_set()
        assert not flow.state(DAY)["jobs"]
    finally:
        release.set()
        worker.join(timeout=5)
        if reset.ident is not None:
            reset.join(timeout=5)
        flow.engine.dispose()


def test_default_reply_deadline_is_ten_hours_after_sending(tmp_path, legacy_flow_scenario, monkeypatch):
    monkeypatch.delenv("FLOW_TEST_OFFER_MINUTES", raising=False)
    flow = FlowTest(f"sqlite:///{(tmp_path / 'deadline.sqlite').as_posix()}", scenario=legacy_flow_scenario)
    try:
        flow.generate(DAY)
        flow.publish(DAY)
        before = datetime.utcnow()
        flow.process_batch()
        after = datetime.utcnow()
        with flow.sessions() as session:
            offers = list(session.scalars(select(FlowOffer)))
            assert all(before + timedelta(hours=10) <= o.expires_at <= after + timedelta(hours=10) for o in offers)
        assert flow.expire_offers() == 0
        with flow.sessions.begin() as session:
            session.get(FlowOffer, offers[0].token).expires_at = datetime.utcnow() - timedelta(seconds=1)
        assert flow.expire_offers() == 1
    finally:
        flow.engine.dispose()
