import json

import pytest

from performance_assistant import adapters
from performance_assistant.adapters import MCP, AdapterError, Deadline
from performance_assistant.config import MAX_INPUT
from performance_assistant.contracts import Finding, Plan, Report, TaskPatch
from performance_assistant.conversation import Model
from performance_assistant.workflow import validate_report


def _policy():
    return {
        "mcp_url": "https://profiler.example/mcp",
        "read_tools": ("read_profile",),
        "profile_tool": "read_profile",
        "profile_argument": "profile_id",
        "profile_args": {},
        "identity_path": "",
    }


def test_mcp_enforces_output_schema_on_structured_content(monkeypatch):
    calls = []

    def transport(url, body, headers, deadline):
        calls.append(body["method"])
        if body["method"] == "tools/call":
            result = {"structuredContent": {"profile": 42}}
        else:
            result = {}
        return {"jsonrpc": "2.0", "id": body["id"], "result": result}, {}

    monkeypatch.setattr(adapters, "bounded_json", transport)
    client = MCP(_policy(), None, Deadline(10))
    catalog = {
        "read_profile": {
            "inputSchema": {"type": "object"},
            "outputSchema": {
                "type": "object",
                "properties": {"profile": {"type": "string"}},
                "required": ["profile"],
                "additionalProperties": False,
            },
        }
    }

    with pytest.raises(AdapterError, match="tool_output_invalid"):
        client.call(catalog, "read_profile", {"profile_id": "p"})
    assert calls == ["tools/call"]


def test_catalog_hash_tracks_schema_drift_for_allowed_tools():
    original = {
        "read_profile": {
            "name": "read_profile",
            "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}},
        },
        "not_allowed": {"name": "not_allowed", "inputSchema": {"type": "object"}},
    }
    changed = json.loads(json.dumps(original))
    changed["read_profile"]["inputSchema"]["properties"]["id"]["minLength"] = 1
    changed["not_allowed"]["description"] = "unrelated server change"

    assert adapters.catalog_hash(original, ("read_profile",)) != adapters.catalog_hash(
        changed, ("read_profile",)
    )
    assert adapters.catalog_hash(original, ("read_profile",)) == adapters.catalog_hash(
        {**original, "not_allowed": {"name": "not_allowed", "inputSchema": {"type": "string"}}},
        ("read_profile",),
    )


def test_retried_replanning_produces_same_question_id(system, monkeypatch):
    config, store, vault, service, worker, cid = system
    config.model_url = "https://model.example"
    config.model_name = "test-model"
    service.patch(cid, "task", TaskPatch(expected_version=0, summary="latency", profile_id="p"))
    rid = service.run(cid, "run")["id"]
    run = store.one("SELECT * FROM runs WHERE id=?", (rid,))
    policy = json.loads(run["policy"])
    policy.update({"model_url": config.model_url, "model_name": config.model_name})
    run["policy"] = json.dumps(policy)
    checkpoint = json.loads(run["checkpoint"])
    checkpoint.update({"phase": "replan", "calls": 0, "planning_rounds": 0})
    run["checkpoint"] = json.dumps(checkpoint)
    monkeypatch.setattr(
        Model,
        "complete",
        lambda *args, **kwargs: Plan(question="Which metric?").model_dump_json(),
    )

    first = worker.analysis.step(run, Deadline(10))["question"]
    second = worker.analysis.step(run, Deadline(10))["question"]
    assert first["id"] == second["id"] == f"{rid}:0:plan:0"
    assert first["prompt"] == second["prompt"] == "Which metric?"


def test_model_input_budget_rejects_before_transport(system, monkeypatch):
    config, _, vault, *_ = system
    config.model_url = "https://model.example"
    config.model_name = "test-model"
    model = Model(config, vault)
    sent = []
    monkeypatch.setattr("performance_assistant.conversation.bounded_json", lambda *args: sent.append(args))

    with pytest.raises(AdapterError, match="model_input_budget"):
        model.complete("Report", {"payload": "x" * MAX_INPUT}, Deadline(10))

    assert sent == []


def test_report_rejects_citations_to_documents_and_unknown_evidence():
    scope = "run-1:0"
    report = Report(
        scope=scope,
        findings=[Finding(title="claim", observation="observation", citations=["doc-1"])],
    )
    evidence = [
        {
            "id": "doc-1",
            "scope": scope,
            "metadata": json.dumps({"kind": "documentation"}),
        },
        {
            "id": "measure-1",
            "scope": scope,
            "metadata": json.dumps({"kind": "profile"}),
        },
    ]

    with pytest.raises(AdapterError, match="invalid_citation"):
        validate_report(report, scope, evidence)

    report.findings[0].citations = ["unknown"]
    with pytest.raises(AdapterError, match="invalid_citation"):
        validate_report(report, scope, evidence)
