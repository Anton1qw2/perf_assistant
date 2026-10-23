import asyncio
import json
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from .config import MAX_INPUT, Config
from .contracts import RunContext, TaskPatch
from .secrets import Vault
from .service import Service
from .storage import Conflict, Store, digest, dumps


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=100)


class MessageCommand(Command):
    text: str = Field(min_length=1, max_length=MAX_INPUT)


class PatchCommand(Command):
    patch: TaskPatch


class SettingsCommand(Command):
    expected_revision: int
    repository: str | None = Field(default=None, max_length=1024)
    ref: str = Field(default="HEAD", max_length=256)
    profile_id: str | None = Field(default=None, max_length=256)
    token: str | None = Field(default=None, max_length=8192)
    clear_token: bool = False


class CheckCommand(Command):
    sources: list[Literal["profiler", "git", "docs"]] = Field(
        default=["profiler", "git", "docs"], min_length=1, max_length=3
    )
    expected_settings_revision: int


class AnswerCommand(Command):
    answer: str = Field(min_length=1, max_length=4000)


class ContextCommand(Command):
    context: RunContext


def create_app(config=None):
    config = config or Config.from_env()
    store = Store(config.data_dir)
    vault = Vault(store)
    vault.import_environment(config)
    service = Service(store, config, vault)
    # Settings/policy changes invalidate all current connection statuses on startup.
    fingerprint = digest(
        {
            "policy": config.policy(),
            "credential_refs": [vault.default_ref(s) for s in ("mcp", "model", "executor")],
        }
    )
    with store.tx() as db:
        old = db.execute("SELECT value FROM app_meta WHERE key='policy_fingerprint'").fetchone()
        if old and old["value"] != fingerprint:
            for row in db.execute("SELECT id,settings FROM conversations").fetchall():
                settings = json.loads(row["settings"])
                settings["revision"] += 1
                db.execute(
                    "UPDATE conversations SET settings=? WHERE id=?", (dumps(settings), row["id"])
                )
                store.event(db, row["id"], "settings.updated", {"revision": settings["revision"]})
        db.execute("INSERT OR REPLACE INTO app_meta VALUES('policy_fingerprint',?)", (fingerprint,))
    app = FastAPI(title="Performance Assistant", version="0.1.0")
    app.state.store, app.state.service, app.state.config = store, service, config

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        host = request.headers.get("host", "").split(":")[0]
        if host not in ("127.0.0.1", "localhost", "testserver"):
            return JSONResponse({"code": "loopback_host_required"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin != f"{request.url.scheme}://{request.headers.get('host')}":
            return JSONResponse({"code": "cross_origin_rejected"}, status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                return JSONResponse({"code": "json_required"}, status_code=415)
            if int(request.headers.get("content-length", "0")) > 65536:
                return JSONResponse({"code": "request_too_large"}, status_code=413)
            body = await request.body()
            if len(body) > 65536:
                return JSONResponse({"code": "request_too_large"}, status_code=413)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(Conflict)
    async def conflict_handler(request, exc):
        return JSONResponse({"code": str(exc)}, status_code=409)

    @app.exception_handler(ValueError)
    async def value_handler(request, exc):
        return JSONResponse({"code": "invalid_command"}, status_code=422)

    @app.exception_handler(KeyError)
    async def missing_handler(request, exc):
        return JSONResponse({"code": "not_found"}, status_code=404)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request, exc):
        # Pydantic's default errors contain input values, potentially tokens.
        return JSONResponse({"code": "invalid_request"}, status_code=422)

    @app.exception_handler(Exception)
    async def failure_handler(request, exc):
        return JSONResponse({"code": "internal_error"}, status_code=500)

    @app.get("/api/health")
    def health():
        return {
            "status": "ok",
            "version": "0.1.0",
            "deployment": "local",
            "model_configured": bool(config.model_url and config.model_name),
            "profiler_configured": bool(config.mcp_url),
            "synthetic": False,
        }

    @app.get("/api/conversations")
    def conversations():
        return store.rows("SELECT id,title,created FROM conversations ORDER BY created DESC")

    @app.post("/api/conversations")
    def create():
        return {"id": store.create_conversation()}

    @app.get("/api/conversations/{cid}")
    def snapshot(cid: str):
        return store.snapshot(cid)

    @app.post("/api/conversations/{cid}/messages")
    def message(cid: str, command: MessageCommand):
        return service.intake(cid, command.request_id, command.text)

    @app.patch("/api/conversations/{cid}/task")
    def task(cid: str, command: PatchCommand):
        return service.patch(cid, command.request_id, command.patch)

    @app.put("/api/conversations/{cid}/settings")
    def settings(cid: str, command: SettingsCommand):
        values = command.model_dump(include={"repository", "ref", "profile_id"}, exclude_unset=True)
        return service.save_settings(
            cid,
            command.request_id,
            command.expected_revision,
            values,
            command.token,
            command.clear_token,
        )

    @app.post("/api/conversations/{cid}/runs")
    def run(cid: str, command: Command):
        return service.run(cid, command.request_id)

    @app.post("/api/conversations/{cid}/cancel")
    def cancel(cid: str, command: Command):
        return service.cancel(cid, command.request_id)

    @app.post("/api/conversations/{cid}/questions/{qid}/answer")
    def answer(cid: str, qid: str, command: AnswerCommand):
        return service.answer(cid, command.request_id, qid, command.answer)

    @app.post("/api/conversations/{cid}/runs/{rid}/context")
    def context(cid: str, rid: str, command: ContextCommand):
        return service.propose_context(cid, command.request_id, rid, command.context)

    @app.post("/api/conversations/{cid}/connection-checks")
    def check(cid: str, command: CheckCommand):
        return service.check(
            cid, command.request_id, command.sources, command.expected_settings_revision
        )

    @app.get("/api/conversations/{cid}/connection-checks")
    def checks(cid: str):
        return store.snapshot(cid)["checks"]

    @app.get("/api/conversations/{cid}/evidence/{eid}")
    def evidence(cid: str, eid: str):
        row = store.one(
            "SELECT e.* FROM evidence e JOIN runs r ON r.id=e.run_id WHERE e.id=? AND r.conversation_id=?",
            (eid, cid),
        )
        if not row:
            raise KeyError()
        return {"metadata": json.loads(row["metadata"]), "payload": json.loads(row["payload"])}

    @app.get("/api/conversations/{cid}/events")
    async def events(cid: str, request: Request, after: int = 0):
        if not store.one("SELECT id FROM conversations WHERE id=?", (cid,)):
            raise KeyError()
        try:
            cursor = max(after, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            cursor = after

        async def stream():
            nonlocal cursor
            while not await request.is_disconnected():
                rows = store.rows(
                    "SELECT * FROM events WHERE conversation_id=? AND id>? ORDER BY id LIMIT 100",
                    (cid, cursor),
                )
                for event in rows:
                    cursor = event["id"]
                    yield f"id: {cursor}\ndata: {dumps({'kind': event['kind'], 'payload': json.loads(event['payload'])})}\n\n"
                if not rows:
                    yield ": heartbeat\n\n"
                    await asyncio.sleep(1)

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
        )

    web = Path(__file__).parent / "web"

    @app.get("/")
    def index():
        return FileResponse(web / "index.html")

    @app.get("/assets/{filename}")
    def assets(filename: str):
        if filename not in ("app.js", "state.mjs", "markdown.mjs", "style.css"):
            raise KeyError()
        return FileResponse(web / filename)

    return app
