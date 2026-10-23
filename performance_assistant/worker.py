import json
import time

from .adapters import AdapterError, Deadline
from .config import TURN_SECONDS
from .contracts import TaskPatch, TaskSpec, patch_task
from .conversation import Model, classify, consultation, prepare, respond, route
from .diagnostics import describe, probe
from .service import Service
from .storage import Conflict, dumps, uid
from .workflow import Analysis, HTTPExecutor


class Worker:
    def __init__(self, store, config, vault):
        self.store, self.config, self.vault = store, config, vault
        self.owner = uid()
        self.service = Service(store, config, vault)
        self.model = Model(config, vault)
        self.analysis = Analysis(store, config, vault, self.model)

    def tick(self):
        job = self.store.claim(self.owner)
        if not job:
            return False
        started = time.monotonic()
        try:
            remaining = min(TURN_SECONDS, job["deadline"] - time.time())
            deadline = Deadline(remaining)
            deadline.remaining()
            payload = json.loads(job["payload"])
            if job["kind"] == "intake":
                self.intake(job, payload, deadline)
            elif job["kind"] == "diagnostics":
                self.diagnostics(job, payload, deadline)
            elif job["kind"] == "run":
                self.run(job, payload, started)
            elif job["kind"] == "external_cancel":
                token = self.vault.resolve(
                    self.vault.default_ref("executor"), self.config.executor_url
                )
                HTTPExecutor(self.config.executor_url, token, deadline).cancel(payload["run_id"])
                with self.store.owned(job) as db:
                    self.finish(db, job)
            else:
                raise AdapterError("unknown_job_kind")
        except Conflict:
            pass  # Lease fencing intentionally rejects all stale results.
        except Exception as error:
            code = error.code if isinstance(error, AdapterError) else "operation_failed"
            try:
                with self.store.owned(job) as db:
                    db.execute("UPDATE jobs SET status='failed' WHERE id=?", (job["id"],))
                    if job["kind"] == "run":
                        rid = json.loads(job["payload"])["run_id"]
                        db.execute(
                            "UPDATE runs SET status='failed',failure=?,active_seconds=active_seconds+? WHERE id=?",
                            (code, time.monotonic() - started, rid),
                        )
                    self.store.event(
                        db, job["conversation_id"], "job.failed", {"id": job["id"], "code": code}
                    )
                    self.store.message(
                        db,
                        job["conversation_id"],
                        "assistant",
                        "Операция остановлена: " + code,
                        "system",
                    )
            except Conflict:
                pass
        return True

    def finish(self, db, job):
        # owned() checks the lease again on exit; completion is deferred by its commit check.
        db.execute("UPDATE jobs SET status='done' WHERE id=?", (job["id"],))
        self.store.event(db, job["conversation_id"], "job.completed", {"id": job["id"]})

    def intake(self, job, payload, deadline):
        cid = job["conversation_id"]
        snap = self.store.snapshot(cid)
        pending = self.store.one(
            "SELECT * FROM consultations WHERE conversation_id=? AND status='pending' ORDER BY created DESC LIMIT 1",
            (cid,),
        )
        explicit_switch = payload["text"].startswith("/") or classify(payload["text"]) in (
            "diagnostics",
            "capabilities",
            "task",
        )
        if pending and not explicit_switch:
            classification = "consultation"
        else:
            classification = route(
                payload["text"], TaskSpec.model_validate(snap["task"]), self.model, deadline
            )
        patch = None
        followup = None
        if classification in ("task", "mixed"):
            updates = prepare(
                payload["text"], TaskSpec.model_validate(snap["task"]), self.model, deadline
            )
            patch = TaskPatch(expected_version=payload["task_version"], **updates)
            reply = (
                "Задача подготовлена. Проверьте цель и параметры; исследование начнётся после Run."
            )
            if classification == "mixed":
                reply += "\n\n" + respond(payload["text"], "consultation", self.model, deadline)
        elif classification == "diagnostics":
            reply = "Проверка подключений поставлена в очередь."
        elif classification == "consultation":
            result = consultation(
                payload["text"],
                self.model,
                deadline,
                pending["prompt"] if pending and not explicit_switch else None,
            )
            reply, followup = result.text, result.question
            if followup:
                reply += "\n\n" + followup
        else:
            reply = respond(payload["text"], classification, self.model, deadline)
        deadline.remaining()
        with self.store.owned(job) as db:
            row = self.service.conversation(db, cid)
            if pending:
                db.execute(
                    "UPDATE consultations SET answer=?,status=? WHERE id=?",
                    (
                        payload["text"] if not explicit_switch else None,
                        "answered" if not explicit_switch else "superseded",
                        pending["id"],
                    ),
                )
            if followup:
                db.execute(
                    "INSERT INTO consultations VALUES(?,?,?,?,?,?)",
                    (
                        payload["message_id"] + ":consult",
                        cid,
                        followup,
                        None,
                        "pending",
                        time.time(),
                    ),
                )
            if patch:
                try:
                    task = patch_task(TaskSpec.model_validate_json(row["task"]), patch)
                    db.execute(
                        "UPDATE conversations SET task=? WHERE id=?", (task.model_dump_json(), cid)
                    )
                    self.store.event(db, cid, "task.updated", {"version": task.spec_version})
                except ValueError:
                    reply = "Задача уже изменена. Повторите запрос к текущей версии."
            if classification == "diagnostics":
                sources = ["profiler", "git", "docs"]
                parts = payload["text"].split()
                if len(parts) == 2 and parts[0] == "/check" and parts[1] in sources:
                    sources = [parts[1]]
                self.service.enqueue_check(
                    db, cid, sources, json.loads(row["settings"]), job["deadline"]
                )
            db.execute(
                "UPDATE messages SET classification=? WHERE id=?",
                (classification, payload["message_id"]),
            )
            self.store.message(db, cid, "assistant", reply, classification, payload["message_id"])
            self.finish(db, job)

    def diagnostics(self, job, payload, deadline):
        settings = payload["settings"]
        token = None
        credential_error = False
        try:
            ref = settings.get("credential_ref")
            token = self.vault.resolve(
                ref or self.vault.default_ref("mcp"),
                payload["policy"]["mcp_url"],
                settings.get("repository") if ref else None,
            )
        except ValueError:
            credential_error = True
        checks = []
        for index, source in enumerate(payload["sources"]):
            source_deadline = Deadline(
                min(30, deadline.remaining() / (len(payload["sources"]) - index + 1))
            )
            if source == "profiler" and credential_error:
                # Never quietly fall back to an anonymous probe after binding failure.
                from .contracts import ConnectionCheck, Stage

                checks.append(
                    ConnectionCheck(
                        id=uid(),
                        source=source,
                        revision=settings["revision"],
                        scope=settings.get("profile_id") or "no_selection",
                        stages={
                            "authorization": Stage(
                                outcome="fail", reason="credential_binding_mismatch"
                            )
                        },
                        checked_at=time.time(),
                        expires_at=time.time() + 300,
                    )
                )
            else:
                checks.append(
                    probe(source, settings, payload["policy"], self.config, token, source_deadline)
                )
        deadline.remaining()
        with self.store.owned(job) as db:
            for check in checks:
                db.execute(
                    "INSERT INTO checks VALUES(?,?,?,?,?)",
                    (
                        check.id,
                        job["conversation_id"],
                        check.source,
                        check.revision,
                        check.model_dump_json(),
                    ),
                )
            current = json.loads(self.service.conversation(db, job["conversation_id"])["settings"])
            if current["revision"] == settings["revision"]:
                self.store.message(
                    db,
                    job["conversation_id"],
                    "assistant",
                    "\n\n".join(describe(c) for c in checks),
                    "diagnostics",
                )
            self.store.event(
                db,
                job["conversation_id"],
                "connection-check.completed",
                {"revision": settings["revision"]},
            )
            self.finish(db, job)

    def run(self, job, payload, started):
        run = self.store.one("SELECT * FROM runs WHERE id=?", (payload["run_id"],))
        if not run or run["status"] not in ("queued", "running"):
            with self.store.owned(job) as db:
                self.finish(db, job)
            return
        task = TaskSpec.model_validate_json(run["snapshot"])
        remaining = task.options.active_seconds - run["active_seconds"]
        deadline = Deadline(min(remaining, TURN_SECONDS))
        deadline.remaining()
        result = self.analysis.step(run, deadline)
        if self.vault.scrub(dumps(result)) != dumps(result):
            raise AdapterError("credential_in_result_rejected")
        deadline.remaining()
        with self.store.owned(job) as db:
            for item in result["evidence"]:
                meta = item["metadata"]
                db.execute(
                    "INSERT INTO evidence VALUES(?,?,?,?,?)",
                    (meta["id"], run["id"], meta["scope"], dumps(meta), dumps(item["payload"])),
                )
                result["checkpoint"]["evidence_ids"].append(meta["id"])
            if "report" in result:
                db.execute(
                    "UPDATE runs SET report=? WHERE id=?", (dumps(result["report"]), run["id"])
                )
            db.execute(
                "UPDATE runs SET checkpoint=?,context=?,status=?,active_seconds=active_seconds+? WHERE id=?",
                (
                    dumps(result["checkpoint"]),
                    dumps(result["context"]),
                    result["status"],
                    time.monotonic() - started,
                    run["id"],
                ),
            )
            if result.get("question"):
                q = result["question"]
                db.execute(
                    "INSERT OR IGNORE INTO questions VALUES(?,?,?,?,?)",
                    (q["id"], run["id"], dumps(q), None, time.time()),
                )
                self.store.event(db, run["conversation_id"], "question.created", {"id": q["id"]})
            if result.get("message"):
                self.store.message(
                    db, run["conversation_id"], "assistant", result["message"], "report"
                )
            self.store.event(
                db,
                run["conversation_id"],
                "run.progress",
                {
                    "id": run["id"],
                    "phase": result["checkpoint"]["phase"],
                    "status": result["status"],
                },
            )
            self.finish(db, job)
            if result["status"] == "running":
                self.store.enqueue(
                    db, run["conversation_id"], "run", payload, time.time() + 86400 * 30
                )

    def serve(self):
        while True:
            if not self.tick():
                time.sleep(0.25)
