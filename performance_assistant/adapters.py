"""Bounded transports. Remote content is data, never authority."""

import hashlib
import json
import re
import subprocess
import time
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import httpx
from jsonschema import Draft202012Validator

from .config import CALL_SECONDS, MAX_PAGES, MAX_PAYLOAD, MAX_TOOLS
from .storage import digest


class AdapterError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class Deadline:
    def __init__(self, seconds):
        self.end = time.monotonic() + max(0, seconds)

    def remaining(self):
        remaining = self.end - time.monotonic()
        if remaining <= 0:
            raise AdapterError("deadline_exhausted")
        return remaining


def bounded_json(url, payload, headers, deadline):
    if "Authorization" in headers and urlsplit(url).scheme != "https":
        raise AdapterError("authenticated_https_required")
    timeout = min(CALL_SECONDS, deadline.remaining())
    try:
        with httpx.Client(follow_redirects=False, timeout=timeout, trust_env=False) as client:
            with client.stream("POST", url, json=payload, headers=headers) as response:
                if response.status_code in (401, 403):
                    raise AdapterError("authorization_denied")
                if response.status_code == 404:
                    raise AdapterError("missing_or_forbidden")
                if 300 <= response.status_code < 400:
                    raise AdapterError("redirect_rejected")
                if response.status_code >= 400:
                    raise AdapterError("remote_request_failed")
                data = bytearray()
                for chunk in response.iter_bytes():
                    deadline.remaining()
                    data.extend(chunk)
                    if len(data) > MAX_PAYLOAD:
                        raise AdapterError("payload_limit")
                    # Streamable HTTP supports finite JSON-RPC SSE responses.
                    if "text/event-stream" in response.headers.get("content-type", ""):
                        normalized = data.decode("utf-8", errors="strict").replace("\r\n", "\n")
                        for block in normalized.split("\n\n")[:-1]:
                            parts = [
                                line[5:].strip()
                                for line in block.splitlines()
                                if line.startswith("data:")
                            ]
                            if parts:
                                item = json.loads("\n".join(parts))
                                if item.get("id") == payload.get("id") and (
                                    "result" in item or "error" in item
                                ):
                                    return item, dict(response.headers)
                if not data and "id" not in payload:
                    return {}, dict(response.headers)
                return json.loads(data), dict(response.headers)
    except AdapterError:
        raise
    except httpx.TimeoutException:
        raise AdapterError("transport_timeout") from None
    except (httpx.HTTPError, ValueError, UnicodeError):
        raise AdapterError("transport_or_payload_invalid") from None


def validate_schema(schema):
    def walk(value, depth=0):
        if depth > 32:
            raise AdapterError("schema_depth_limit")
        if isinstance(value, dict):
            for key, child in value.items():
                if key in ("$ref", "$dynamicRef") and (
                    not isinstance(child, str) or not child.startswith("#")
                ):
                    raise AdapterError("remote_schema_reference_rejected")
                walk(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                walk(child, depth + 1)

    walk(schema)
    try:
        Draft202012Validator.check_schema(schema)
    except Exception:
        raise AdapterError("invalid_tool_schema") from None


class MCP:
    def __init__(self, policy, token, deadline):
        self.policy, self.deadline = policy, deadline
        self.url = policy["mcp_url"]
        self.headers = {"Accept": "application/json, text/event-stream"}
        if token:
            self.headers["Authorization"] = f"Bearer {token}"
        self.sequence = 0

    def rpc(self, method, params=None, notification=False):
        self.sequence += 1
        payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notification:
            payload["id"] = self.sequence
        result, headers = bounded_json(self.url, payload, self.headers, self.deadline)
        session = headers.get("mcp-session-id")
        if session:
            self.headers["Mcp-Session-Id"] = session
        if notification:
            return {}
        if not isinstance(result, dict) or result.get("id") != self.sequence:
            raise AdapterError("invalid_rpc_response")
        if "error" in result:
            raise AdapterError("remote_operation_rejected")
        if "result" not in result or not isinstance(result["result"], dict):
            raise AdapterError("invalid_rpc_response")
        return result["result"]

    def discover(self):
        if not self.url:
            raise AdapterError("not_configured")
        init = self.rpc(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "performance-assistant", "version": "0.1.0"},
            },
        )
        version = init.get("protocolVersion")
        if version not in ("2024-11-05", "2025-03-26", "2025-06-18"):
            raise AdapterError("unsupported_mcp_version")
        self.headers["MCP-Protocol-Version"] = version
        self.rpc("notifications/initialized", notification=True)
        catalog, cursor = {}, None
        for _ in range(MAX_PAGES):
            page = self.rpc("tools/list", {"cursor": cursor} if cursor else {})
            tools = page.get("tools")
            if not isinstance(tools, list):
                raise AdapterError("invalid_catalog")
            for tool in tools:
                if (
                    not isinstance(tool, dict)
                    or not isinstance(tool.get("name"), str)
                    or not isinstance(tool.get("inputSchema"), dict)
                ):
                    raise AdapterError("invalid_catalog")
                if tool["name"] in catalog:
                    raise AdapterError("duplicate_tool_name")
                validate_schema(tool["inputSchema"])
                if "outputSchema" in tool:
                    validate_schema(tool["outputSchema"])
                catalog[tool["name"]] = tool
                if len(catalog) > MAX_TOOLS:
                    raise AdapterError("catalog_limit")
            cursor = page.get("nextCursor")
            if not cursor:
                return catalog
        raise AdapterError("discovery_page_limit")

    def profile_call(self, catalog, profile):
        name = self.policy["profile_tool"]
        if not name:
            raise AdapterError("semantic_adapter_not_configured")
        if name not in self.policy["read_tools"] or name not in catalog:
            raise AdapterError("tool_not_allowed_or_missing")
        arguments = {**self.policy["profile_args"], self.policy["profile_argument"]: profile}
        result = self.call(catalog, name, arguments)
        path = self.policy["identity_path"]
        if path:
            identity = result.get("structuredContent")
            for part in path.split("."):
                identity = identity.get(part) if isinstance(identity, dict) else None
            if str(identity) != profile:
                raise AdapterError("profile_identity_mismatch")
        return result

    def call(self, catalog, name, arguments):
        if name not in self.policy["read_tools"] or name not in catalog:
            raise AdapterError("tool_not_allowed_or_missing")
        try:
            Draft202012Validator(catalog[name]["inputSchema"]).validate(arguments)
        except Exception:
            raise AdapterError("tool_arguments_invalid") from None
        result = self.rpc("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise AdapterError("object_missing_or_forbidden")
        if "outputSchema" in catalog[name]:
            try:
                Draft202012Validator(catalog[name]["outputSchema"]).validate(
                    result.get("structuredContent")
                )
            except Exception:
                raise AdapterError("tool_output_invalid") from None
        return result


class Git:
    def __init__(self, executable, repository, deadline):
        self.executable, self.deadline = executable, deadline
        self.repository = str(Path(repository).resolve())

    def run(self, *args):
        # Disable replacement objects, hooks, optional locks and external text conversions.
        import os

        env = {
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
        try:
            with subprocess.Popen(
                [self.executable, "-C", self.repository, "-c", "core.hooksPath=", *args],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
            ) as process:
                try:
                    output, _ = process.communicate(
                        timeout=min(CALL_SECONDS, self.deadline.remaining())
                    )
                except (subprocess.TimeoutExpired, AdapterError):
                    process.kill()
                    process.communicate()
                    raise AdapterError("source_timeout") from None
                if process.returncode:
                    raise AdapterError("repository_or_ref_unreadable")
                if len(output) > MAX_PAYLOAD:
                    raise AdapterError("source_size_limit")
                return output
        except OSError:
            raise AdapterError("git_unavailable") from None

    def resolve(self, ref):
        if not ref or ref.startswith("-") or len(ref) > 256:
            raise AdapterError("invalid_ref")
        commit = (
            self.run("rev-parse", "--verify", "--end-of-options", ref + "^{commit}")
            .decode()
            .strip()
        )
        if not re.fullmatch(r"[a-f0-9]{40,64}", commit):
            raise AdapterError("invalid_commit")
        return commit

    def read(self, commit, path):
        p = PurePosixPath(path)
        if (
            p.is_absolute()
            or ".." in p.parts
            or "\\" in path
            or ":" in path
            or "\x00" in path
            or not path
            or path.startswith("-")
        ):
            raise AdapterError("invalid_source_path")
        listing = self.run("ls-tree", "-z", commit, "--", path).decode("utf-8").rstrip("\0")
        if not listing or "\0" in listing:
            raise AdapterError("source_missing")
        meta, actual = listing.split("\t", 1)
        mode, kind, oid = meta.split(" ")
        if actual != path or mode not in ("100644", "100755") or kind != "blob":
            raise AdapterError("source_not_regular_blob")
        size = int(self.run("cat-file", "-s", oid))
        if size > MAX_PAYLOAD:
            raise AdapterError("source_size_limit")
        try:
            return self.run("cat-file", "blob", oid).decode("utf-8")
        except UnicodeError:
            raise AdapterError("source_not_utf8") from None


def load_document(path):
    try:
        file = Path(path)
        if file.is_symlink() or not file.is_file():
            raise AdapterError("document_unreadable")
        with file.open("rb") as stream:
            content = stream.read(MAX_PAYLOAD + 1)
        if len(content) > MAX_PAYLOAD:
            raise AdapterError("document_size_limit")
        text = content.decode("utf-8-sig")
        if not text.strip():
            raise AdapterError("document_empty")
        return {"text": text, "sha256": hashlib.sha256(content).hexdigest()}
    except (OSError, UnicodeError):
        raise AdapterError("document_unreadable") from None


def catalog_hash(catalog, allowed):
    return digest({k: v for k, v in catalog.items() if k in allowed})
