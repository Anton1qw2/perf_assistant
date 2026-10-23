import json
import time

import pytest
from conftest import drain

from performance_assistant.contracts import TaskPatch
from performance_assistant.storage import Conflict, Store
from performance_assistant.worker import Worker


def test_task_partial_clear_and_stale(system):
    _, store, _, service, _, cid = system
    service.patch(
        cid, "one", TaskPatch(expected_version=0, summary="latency", profile_id="result-1")
    )
    service.patch(cid, "two", TaskPatch(expected_version=1, summary="cpu"))
    task = store.snapshot(cid)["task"]
    assert task["profile_id"] == "result-1" and task["spec_version"] == 2
    with pytest.raises(ValueError):
        service.patch(cid, "bad", TaskPatch(expected_version=0, summary="stale"))
    service.patch(cid, "clear", TaskPatch(expected_version=2, profile_id=None))
    assert store.snapshot(cid)["readiness"] == ["target_required"]


def test_duplicate_command_and_immutable_snapshot(system):
    _, store, _, service, _, cid = system
    service.patch(
        cid, "task", TaskPatch(expected_version=0, summary="latency", profile_id="result")
    )
    first = service.run(cid, "start")
    assert service.run(cid, "start") == first
    with pytest.raises(Conflict):
        service.run(cid, "different")
    service.patch(cid, "edit", TaskPatch(expected_version=1, summary="new"))
    assert store.snapshot(cid)["runs"][0]["snapshot"]["summary"] == "latency"
    with pytest.raises(Conflict):
        service.intake(cid, "start", "other request")


def test_stale_worker_cannot_commit(system):
    _, store, _, service, _, cid = system
    service.intake(cid, "one", "/ask hello")
    first = store.claim("worker-a")
    with store.tx() as db:
        db.execute("UPDATE jobs SET lease_until=? WHERE id=?", (time.time() - 1, first["id"]))
    second = store.claim("worker-b")
    assert second["id"] == first["id"]
    with pytest.raises(Conflict), store.owned(first) as db:
        store.message(db, cid, "assistant", "stale reply", "dialogue")
    with store.owned(second) as db:
        store.message(db, cid, "assistant", "accepted", "dialogue")
        db.execute("UPDATE jobs SET status='done' WHERE id=?", (second["id"],))
    assert [m["text"] for m in store.snapshot(cid)["messages"]] == ["/ask hello", "accepted"]


def test_transaction_rollback_and_restart(system):
    config, store, vault, service, worker, cid = system
    service.intake(cid, "a", "/task investigate latency")
    with pytest.raises(RuntimeError), store.tx() as db:
        store.message(db, cid, "assistant", "should rollback", "dialogue")
        raise RuntimeError("crash before commit")
    restarted = Worker(Store(config.data_dir), config, vault)
    drain(restarted)
    assert store.snapshot(cid)["task"]["summary"] == "investigate latency"
    assert len(store.snapshot(cid)["messages"]) == 2
    drain(worker)
    assert len(store.snapshot(cid)["messages"]) == 2


def test_cancel_fences_running_job(system):
    _, store, _, service, _, cid = system
    service.patch(cid, "task", TaskPatch(expected_version=0, summary="x", profile_id="p"))
    service.run(cid, "run")
    job = store.claim("worker")
    service.cancel(cid, "cancel")
    with pytest.raises(Conflict), store.owned(job):
        pass
    assert store.snapshot(cid)["runs"][0]["status"] == "cancelled"


def test_diagnostics_dedup_and_late_revision(system):
    _, store, _, service, worker, cid = system
    first = service.check(cid, "a", ["profiler"], 0)
    assert service.check(cid, "b", ["profiler"], 0) == first
    service.save_settings(cid, "settings", 0, {"profile_id": "new"})
    drain(worker)
    snap = store.snapshot(cid)
    assert snap["checks"]["profiler"]["stale"]
    assert snap["messages"] == []  # late diagnostic reply isn't presented as current


def test_expired_job_cannot_report_success(system):
    _, store, _, service, worker, cid = system
    result = service.check(cid, "a", ["docs"], 0)
    with store.tx() as db:
        db.execute("UPDATE jobs SET deadline=? WHERE id=?", (time.time() - 1, result["job_id"]))
    drain(worker)
    assert not store.snapshot(cid)["checks"]
    assert store.one("SELECT status FROM jobs")["status"] == "failed"


def test_active_budget_charged_after_abandoned_lease(system):
    _, store, _, service, _, cid = system
    service.patch(cid, "task", TaskPatch(expected_version=0, summary="x", profile_id="p"))
    service.run(cid, "run")
    job = store.claim("a")
    with store.tx() as db:
        db.execute("UPDATE jobs SET lease_until=? WHERE id=?", (time.time() - 1, job["id"]))
    store.claim("b")
    assert store.snapshot(cid)["runs"][0]["active_seconds"] == 150


@pytest.mark.parametrize(
    "text",
    [
        "/ask why slow?",
        "Что ты умеешь?",
        "Объясни latency",
        "/check all",
        "привет",
        "Can you access this repository?",
    ],
)
def test_non_task_isolation(system, text):
    _, store, _, service, worker, cid = system
    service.patch(cid, "task", TaskPatch(expected_version=0, summary="original", profile_id="p"))
    before = store.snapshot(cid)["task"]
    service.intake(cid, "message", text)
    drain(worker)
    assert store.snapshot(cid)["task"] == before
    assert not store.snapshot(cid)["runs"]


def test_question_correlated_and_retry_stable(system):
    _, store, _, service, _, cid = system
    service.patch(cid, "task", TaskPatch(expected_version=0, summary="x", profile_id="p"))
    rid = service.run(cid, "run")["id"]
    with store.tx() as db:
        db.execute("UPDATE runs SET status='waiting' WHERE id=?", (rid,))
        db.execute("UPDATE jobs SET status='done'")
        db.execute(
            "INSERT INTO questions VALUES(?,?,?,?,?)",
            ("q", rid, json.dumps({"prompt": "metric?"}), None, time.time()),
        )
    assert service.intake(cid, "reply", "cpu") == {"id": "q"}
    assert service.answer(cid, "retry", "q", "cpu") == {"id": "q"}
    with pytest.raises(Conflict):
        service.answer(cid, "different", "q", "memory")
    assert len(store.rows("SELECT * FROM jobs WHERE status='queued'")) == 1
