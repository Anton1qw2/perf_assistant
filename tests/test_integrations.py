import json
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import drain

from performance_assistant import adapters
from performance_assistant.adapters import MCP, AdapterError, Deadline, Git, validate_schema
from performance_assistant.contracts import Finding, Report, TaskPatch
from performance_assistant.diagnostics import probe
from performance_assistant.workflow import validate_report

CATALOG = {
    "tools": [
        {
            "name": "read_profile",
            "inputSchema": {
                "type": "object",
                "properties": {"profile_id": {"type": "string"}},
                "required": ["profile_id"],
                "additionalProperties": False,
            },
        }
    ]
}


def transport(url, body, headers, deadline):
    method = body["method"]
    if method == "initialize":
        result = {"protocolVersion": "2025-03-26"}
    elif method == "notifications/initialized":
        return {}, {}
    elif method == "tools/list":
        result = CATALOG
    else:
        result = {
            "structuredContent": {
                "id": body["params"]["arguments"]["profile_id"],
                "hotspots": [{"name": "work", "samples": 0, "active": False}],
            },
            "content": [],
        }
    return {"jsonrpc": "2.0", "id": body["id"], "result": result}, {}


def configure(config):
    config.mcp_url = "https://profiler.example/mcp"
    config.read_tools = ("read_profile",)
    config.profile_tool = "read_profile"
    config.identity_path = "id"


def test_mcp_real_path_with_synthetic_transport(system, monkeypatch):
    config, store, _, service, worker, cid = system
    configure(config)
    monkeypatch.setattr(adapters, "bounded_json", transport)
    service.patch(
        cid, "task", TaskPatch(expected_version=0, summary="latency", profile_id="opaque-result")
    )
    service.run(cid, "run")
    drain(worker)
    run = store.snapshot(cid)["runs"][0]
    assert run["status"] == "completed", run
    assert run["report"]["correctness"] == "unknown"
    assert run["report"]["measured_improvement"] == "unknown"
    evidence = store.rows("SELECT * FROM evidence")
    assert any('"samples":0' in e["payload"] and '"active":false' in e["payload"] for e in evidence)
    assert any(json.loads(e["metadata"])["kind"] == "hotspot_record" for e in evidence)


def test_protocol_success_does_not_prove_object_access(system, monkeypatch):
    config, _, _, _, _, _ = system
    configure(config)
    config.profile_tool = ""
    monkeypatch.setattr(adapters, "bounded_json", transport)
    check = probe(
        "profiler", {"revision": 0, "profile_id": "p"}, config.policy(), config, None, Deadline(10)
    )
    assert check.stages["protocol"].outcome == "pass"
    assert check.stages["authorization"].outcome == "unknown"
    assert check.stages["selected_object"].outcome == "unknown"


def test_object_denied_separate_from_protocol(system, monkeypatch):
    config, _, _, _, _, _ = system
    configure(config)

    def denied(url, body, headers, deadline):
        if body["method"] == "tools/call":
            raise AdapterError("authorization_denied")
        return transport(url, body, headers, deadline)

    monkeypatch.setattr(adapters, "bounded_json", denied)
    check = probe(
        "profiler", {"revision": 0, "profile_id": "p"}, config.policy(), config, None, Deadline(10)
    )
    assert check.stages["protocol"].outcome == "pass"
    assert check.stages["authorization"].outcome == "fail"
    assert check.stages["selected_object"].outcome == "fail"


def test_schema_external_refs_never_retrieved():
    with pytest.raises(AdapterError, match="remote_schema_reference"):
        validate_schema({"$ref": "https://evil.example/schema"})


def test_discovery_bounds(system, monkeypatch):
    config, *_ = system
    configure(config)

    def endless(url, body, headers, deadline):
        if body["method"] == "tools/list":
            return {"id": body["id"], "result": {"tools": [], "nextCursor": "next"}}, {}
        return transport(url, body, headers, deadline)

    monkeypatch.setattr(adapters, "bounded_json", endless)
    with pytest.raises(AdapterError, match="discovery_page_limit"):
        MCP(config.policy(), None, Deadline(10)).discover()


def test_report_rejects_missing_and_wrong_scope_citations():
    report = Report(
        scope="run:0", findings=[Finding(title="x", observation="x", citations=["invented"])]
    )
    with pytest.raises(AdapterError, match="invalid_citation"):
        validate_report(report, "run:0", [])
    with pytest.raises(AdapterError, match="scope"):
        validate_report(report, "run:1", [])


def test_git_immutable_blob_and_path_bounds(tmp_path):
    executable = shutil.which("git")
    if not executable:
        candidate = (
            Path.home()
            / ".cache/codex-runtimes/codex-primary-runtime/dependencies/native/git/cmd/git.exe"
        )
        if candidate.exists():
            executable = str(candidate)
    if not executable:
        pytest.skip("git unavailable")
    subprocess.run([executable, "init", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / "code.py").write_text("old = 1\n", encoding="utf-8")
    subprocess.run(
        [executable, "-C", str(tmp_path), "add", "code.py"], check=True, capture_output=True
    )
    subprocess.run(
        [
            executable,
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.test",
            "commit",
            "-m",
            "initial",
        ],
        check=True,
        capture_output=True,
    )
    git = Git(executable, str(tmp_path), Deadline(10))
    commit = git.resolve("HEAD")
    (tmp_path / "code.py").write_text("changed = 2\n", encoding="utf-8")
    assert git.read(commit, "code.py") == "old = 1\n"
    for bad in ("../secret", "C:/secret", "/etc/passwd", "dir\\file", "-bad"):
        with pytest.raises(AdapterError):
            git.read(commit, bad)


def test_format_restart_does_not_repeat_transport(system, monkeypatch):
    config, store, vault, service, worker, cid = system
    configure(config)
    calls = []

    def tracked(*args):
        calls.append(args[1]["method"])
        return transport(*args)

    monkeypatch.setattr(adapters, "bounded_json", tracked)
    service.patch(cid, "task", TaskPatch(expected_version=0, summary="x", profile_id="p"))
    service.run(cid, "run")
    for _ in range(30):
        worker.tick()
        if store.snapshot(cid)["runs"][0]["checkpoint"]["phase"] == "format_report":
            break
    before = len(calls)
    from performance_assistant.worker import Worker

    drain(Worker(store, config, vault))
    assert len(calls) == before
    assert store.snapshot(cid)["runs"][0]["status"] == "completed"
