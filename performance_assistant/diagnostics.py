import time

from .adapters import MCP, AdapterError, Git, load_document
from .config import FRESH_SECONDS
from .contracts import ConnectionCheck, Stage
from .storage import uid

STAGES = (
    "configuration",
    "protocol",
    "authorization",
    "permitted_tools",
    "selected_object",
    "documentation",
)


def probe(source, settings, policy, config, token, deadline):
    stages = {name: Stage() for name in STAGES}

    def set_stage(name, outcome, reason):
        stages[name] = Stage(outcome=outcome, reason=reason)

    scope = (
        settings.get("profile_id")
        if source == "profiler"
        else settings.get("repository")
        if source == "git"
        else "configured_document"
    )
    check = ConnectionCheck(
        id=uid(),
        source=source,
        revision=settings["revision"],
        scope=scope or "no_selection",
        stages=stages,
        checked_at=time.time(),
        expires_at=time.time() + FRESH_SECONDS,
    )
    stages = check.stages
    current = "configuration"
    try:
        if source == "profiler":
            if not policy["mcp_url"]:
                set_stage("configuration", "fail", "not_configured")
                return check
            set_stage("configuration", "pass", "settings_present")
            current = "protocol"
            client = MCP(policy, token, deadline)
            catalog = client.discover()
            set_stage("protocol", "pass", "mcp_discovered")
            overlap = set(catalog) & set(policy["read_tools"])
            set_stage(
                "permitted_tools",
                "pass" if overlap else "fail",
                "allowed_tools_present" if overlap else "no_allowed_tools",
            )
            set_stage("authorization", "unknown", "discovery_does_not_prove_object_authorization")
            set_stage("documentation", "not_applicable", "separate_source")
            if not settings.get("profile_id"):
                set_stage("selected_object", "skipped", "no_selection")
            elif not policy["profile_tool"]:
                set_stage("selected_object", "unknown", "semantic_adapter_not_configured")
            else:
                current = "selected_object"
                client.profile_call(catalog, settings["profile_id"])
                set_stage("authorization", "pass", "tested_read_accepted")
                set_stage(
                    "selected_object",
                    "pass",
                    "identity_verified"
                    if policy["identity_path"]
                    else "configured_read_accepted_identity_unverified",
                )
        elif source == "git":
            if not settings.get("repository"):
                set_stage("configuration", "fail", "no_selection")
                return check
            set_stage("configuration", "pass", "selection_present")
            current = "selected_object"
            commit = Git(config.git, settings["repository"], deadline).resolve(
                settings.get("ref", "HEAD")
            )
            check.scope = commit
            set_stage("selected_object", "pass", "commit_readable_build_linkage_unknown")
            set_stage("authorization", "pass", "local_read_accepted")
            for name in ("protocol", "permitted_tools", "documentation"):
                set_stage(name, "not_applicable", "local_git")
        else:
            if not policy["document"]:
                set_stage("configuration", "fail", "not_configured")
                return check
            set_stage("configuration", "pass", "settings_present")
            current = "documentation"
            document = load_document(policy["document"])
            check.scope = document["sha256"]
            set_stage("documentation", "pass", "full_document_readable")
            for name in ("protocol", "authorization", "permitted_tools", "selected_object"):
                set_stage(name, "not_applicable", "local_document")
    except AdapterError as error:
        set_stage(current, "fail", error.code)
        if error.code == "authorization_denied":
            set_stage("authorization", "fail", error.code)
    return check


def describe(check):
    outcomes = ", ".join(
        f"{name}: {stage.outcome} ({stage.reason})"
        for name, stage in check.stages.items()
        if stage.outcome != "not_applicable"
    )
    return f"{check.source} · {check.scope}\n{outcomes}"
