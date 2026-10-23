import json
import time

from .contracts import Question, RunContext, Settings, TaskSpec, patch_task
from .storage import Conflict, digest, dumps, uid


class Service:
    def __init__(self, store, config, vault):
        self.store, self.config, self.vault = store, config, vault

    @staticmethod
    def conversation(db, cid):
        row = db.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
        if not row:
            raise KeyError("conversation_not_found")
        return row

    def command(self, cid, request_id, body, operation):
        serialized = dumps(body)
        if self.vault.scrub(serialized) != serialized:
            raise ValueError("credential_in_command_rejected")
        with self.store.tx() as db:
            self.conversation(db, cid)
            return self.store.dedup(db, cid, request_id, body, lambda: operation(db))

    def patch(self, cid, request_id, patch):
        def op(db):
            row = self.conversation(db, cid)
            task = patch_task(TaskSpec.model_validate_json(row["task"]), patch)
            db.execute("UPDATE conversations SET task=? WHERE id=?", (task.model_dump_json(), cid))
            self.store.event(db, cid, "task.updated", {"version": task.spec_version})
            return task.model_dump()

        return self.command(
            cid, request_id, {"type": "patch", **patch.model_dump(exclude_unset=True)}, op
        )

    def save_settings(self, cid, request_id, expected, values, token=None, clear_token=False):
        # Never fingerprint or persist token-containing request bodies.
        def op(db):
            row = self.conversation(db, cid)
            old = Settings.model_validate_json(row["settings"])
            if expected != old.revision:
                raise Conflict("settings_version_conflict")
            settings = Settings.model_validate(
                {**old.model_dump(), **values, "revision": old.revision + 1}
            )
            TaskSpec(profile_id=settings.profile_id)
            if settings.repository != old.repository or clear_token:
                settings.credential_ref = None
            if token:
                settings.credential_ref = self.vault.save(
                    db, token, self.config.mcp_url, settings.repository
                )
            db.execute(
                "UPDATE conversations SET settings=? WHERE id=?", (settings.model_dump_json(), cid)
            )
            self.store.event(db, cid, "settings.updated", {"revision": settings.revision})
            return settings.model_dump()

        return self.command(
            cid,
            request_id,
            {
                "type": "settings",
                "expected": expected,
                "values": values,
                "token_supplied": bool(token),
                "clear_token": clear_token,
            },
            op,
        )

    def run(self, cid, request_id):
        def op(db):
            row = self.conversation(db, cid)
            task = TaskSpec.model_validate_json(row["task"])
            if task.readiness():
                raise Conflict(",".join(task.readiness()))
            if db.execute(
                "SELECT 1 FROM runs WHERE conversation_id=? AND status IN ('queued','running','waiting')",
                (cid,),
            ).fetchone():
                raise Conflict("active_run_exists")
            rid = uid()
            context = RunContext(
                profile_id=task.profile_id, repository=task.repository, ref=task.ref
            )
            settings = json.loads(row["settings"])
            # A run target differing from the credential-bound selection cannot receive that credential.
            if settings.get("repository") != task.repository:
                settings["credential_ref"] = None
            settings.update(repository=task.repository, profile_id=task.profile_id, ref=task.ref)
            if not settings.get("credential_ref"):
                settings["credential_ref"] = self.vault.default_ref("mcp")
                settings["credential_global"] = True
            settings["model_credential_ref"] = self.vault.default_ref("model")
            settings["executor_credential_ref"] = self.vault.default_ref("executor")
            db.execute(
                "INSERT INTO runs(id,conversation_id,snapshot,settings,policy,context,checkpoint,status,created) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    rid,
                    cid,
                    task.model_dump_json(),
                    dumps(settings),
                    dumps(self.config.policy()),
                    context.model_dump_json(),
                    dumps(
                        {
                            "version": 1,
                            "phase": "discover",
                            "evidence_ids": [],
                            "limitations": [],
                            "calls": 0,
                        }
                    ),
                    "queued",
                    time.time(),
                ),
            )
            self.store.enqueue(db, cid, "run", {"run_id": rid}, time.time() + 86400 * 30)
            self.store.event(db, cid, "run.created", {"id": rid})
            return {"id": rid}

        return self.command(cid, request_id, {"type": "run"}, op)

    def cancel(self, cid, request_id):
        def op(db):
            runs = db.execute(
                "SELECT id FROM runs WHERE conversation_id=? AND status IN ('queued','running','waiting')",
                (cid,),
            ).fetchall()
            for row in runs:
                db.execute("UPDATE runs SET status='cancelled' WHERE id=?", (row["id"],))
            # Cancel all queued/running analysis jobs; fenced workers cannot commit after this.
            db.execute(
                "UPDATE jobs SET status='cancelled' WHERE conversation_id=? AND kind='run' AND status IN ('queued','running')",
                (cid,),
            )
            if runs and self.config.executor_url:
                for row in runs:
                    self.store.enqueue(db, cid, "external_cancel", {"run_id": row["id"]})
            self.store.event(db, cid, "run.cancelled", {})
            return {"cancelled": len(runs)}

        return self.command(cid, request_id, {"type": "cancel"}, op)

    def check(self, cid, request_id, sources, revision):
        def op(db):
            settings = json.loads(self.conversation(db, cid)["settings"])
            if revision != settings["revision"]:
                raise Conflict("settings_version_conflict")
            return self.enqueue_check(db, cid, sources, settings)

        return self.command(
            cid, request_id, {"type": "check", "sources": sources, "revision": revision}, op
        )

    def enqueue_check(self, db, cid, sources, settings, deadline=None):
        payload = {
            "sources": sorted(set(sources)),
            "settings": settings,
            "policy": self.config.policy(),
        }
        for row in db.execute(
            "SELECT id,payload FROM jobs WHERE conversation_id=? AND kind='diagnostics' AND status IN ('queued','running')",
            (cid,),
        ):
            if json.loads(row["payload"]) == json.loads(dumps(payload)):
                return {"job_id": row["id"]}
        return {"job_id": self.store.enqueue(db, cid, "diagnostics", payload, deadline)}

    def answer(self, cid, request_id, question_id, answer):
        answer = self.vault.scrub(answer)

        def op(db):
            q = db.execute(
                "SELECT q.*,r.conversation_id,r.status FROM questions q JOIN runs r ON r.id=q.run_id WHERE q.id=?",
                (question_id,),
            ).fetchone()
            if not q or q["conversation_id"] != cid:
                raise KeyError("question_not_found")
            if q["answer"] is not None:
                if q["answer"] != answer:
                    raise Conflict("question_already_answered")
                return {"id": question_id}
            if q["status"] != "waiting":
                raise Conflict("run_not_waiting")
            question = json.loads(q["payload"])
            if question.get("kind") == "context_confirmation":
                normalized = answer.strip().lower()
                if normalized not in ("yes", "да", "no", "нет"):
                    raise ValueError("confirmation_requires_yes_or_no")
                if normalized in ("yes", "да"):
                    proposal = db.execute(
                        "SELECT payload FROM context_ledger WHERE id=?", (question_id,)
                    ).fetchone()
                    context = json.loads(proposal["payload"])
                    run = db.execute("SELECT * FROM runs WHERE id=?", (q["run_id"],)).fetchone()
                    previous = json.loads(run["context"])
                    settings = json.loads(run["settings"])
                    if context["repository"] != previous["repository"]:
                        # Even global tokens are deliberately not forwarded across a context switch.
                        settings["credential_ref"] = None
                        settings["credential_global"] = False
                    checkpoint = json.loads(run["checkpoint"])
                    fresh = {
                        "version": 1,
                        "phase": "discover",
                        "evidence_ids": [],
                        "limitations": [],
                        "calls": checkpoint.get("calls", 0),
                    }
                    for key in ("source_reads", "planning_rounds", "external_step"):
                        if key in checkpoint:
                            fresh[key] = checkpoint[key]
                    db.execute(
                        "UPDATE runs SET context=?,settings=?,checkpoint=?,report=NULL WHERE id=?",
                        (dumps(context), dumps(settings), dumps(fresh), q["run_id"]),
                    )
            db.execute("UPDATE questions SET answer=? WHERE id=?", (answer, question_id))
            db.execute("UPDATE runs SET status='queued' WHERE id=?", (q["run_id"],))
            self.store.message(db, cid, "user", answer, "execution_answer", question_id)
            self.store.enqueue(db, cid, "run", {"run_id": q["run_id"]}, time.time() + 86400 * 30)
            self.store.event(db, cid, "question.answered", {"id": question_id})
            return {"id": question_id}

        return self.command(
            cid, request_id, {"type": "answer", "question_id": question_id, "answer": answer}, op
        )

    def intake(self, cid, request_id, text):
        text = self.vault.scrub(text)
        previous = self.store.one(
            "SELECT * FROM requests WHERE conversation_id=? AND request_id=?", (cid, request_id)
        )
        if previous:
            result = json.loads(previous["result"])
            if previous["fingerprint"] == digest(
                {"type": "answer", "question_id": result.get("id"), "answer": text}
            ):
                return result
        pending = self.store.one(
            "SELECT q.id FROM questions q JOIN runs r ON r.id=q.run_id WHERE r.conversation_id=? AND r.status='waiting' AND q.answer IS NULL ORDER BY q.created LIMIT 1",
            (cid,),
        )
        if pending:
            return self.answer(cid, request_id, pending["id"], text)
        if text.strip().lower() == "/run":
            return self.run(cid, request_id)
        if text.strip().lower() == "/cancel":
            return self.cancel(cid, request_id)

        def op(db):
            task = json.loads(self.conversation(db, cid)["task"])
            mid = self.store.message(db, cid, "user", text, "pending")
            jid = self.store.enqueue(
                db,
                cid,
                "intake",
                {"message_id": mid, "text": text, "task_version": task["spec_version"]},
            )
            return {"job_id": jid, "message_id": mid}

        return self.command(cid, request_id, {"type": "intake", "text": text}, op)

    def propose_context(self, cid, request_id, run_id, context):
        def op(db):
            run = db.execute(
                "SELECT * FROM runs WHERE id=? AND conversation_id=?", (run_id, cid)
            ).fetchone()
            if not run or run["status"] not in ("queued", "running", "waiting"):
                raise Conflict("context_change_requires_active_run")
            if db.execute(
                "SELECT 1 FROM questions WHERE run_id=? AND answer IS NULL", (run_id,)
            ).fetchone():
                raise Conflict("answer_pending_question_first")
            old_context = json.loads(run["context"])
            if old_context["revision"] >= 16:
                raise Conflict("context_revision_limit")
            if context.revision != old_context["revision"]:
                raise Conflict("context_version_conflict")
            TaskSpec(profile_id=context.profile_id)
            if not context.profile_id and not context.repository:
                raise ValueError("context_target_required")
            context.revision = old_context["revision"] + 1
            context.pinned_commit = None
            # Fence any in-flight step before asking for confirmation. Charge its active lease.
            active = db.execute(
                "SELECT created,lease_until FROM jobs WHERE conversation_id=? AND kind='run' AND status='running'",
                (cid,),
            ).fetchone()
            if active:
                from .config import LEASE_SECONDS

                charged = max(
                    0, min(LEASE_SECONDS, time.time() - (active["lease_until"] - LEASE_SECONDS))
                )
                db.execute(
                    "UPDATE runs SET active_seconds=active_seconds+? WHERE id=?", (charged, run_id)
                )
            db.execute(
                "UPDATE jobs SET status='cancelled' WHERE conversation_id=? AND kind='run' AND status IN ('queued','running')",
                (cid,),
            )
            db.execute("UPDATE runs SET status='waiting' WHERE id=?", (run_id,))
            qid = uid()
            question = Question(
                id=qid,
                kind="context_confirmation",
                owner=run_id,
                prompt="Подтвердите новый контекст: " + dumps(context.model_dump()) + " (yes / no)",
                expected="yes/no",
                continuation="discover",
            )
            db.execute(
                "INSERT INTO questions VALUES(?,?,?,?,?)",
                (qid, run_id, question.model_dump_json(), None, time.time()),
            )
            db.execute(
                "INSERT INTO context_ledger VALUES(?,?,?,?,?)",
                (qid, run_id, context.revision, context.model_dump_json(), time.time()),
            )
            self.store.event(db, cid, "question.created", {"id": qid})
            return question.model_dump()

        return self.command(
            cid,
            request_id,
            {"type": "context", "run_id": run_id, "context": context.model_dump()},
            op,
        )
