import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

MAX_PAYLOAD = 1_000_000
MAX_TOOLS = 128
MAX_PAGES = 4
MAX_CALLS = 8
MAX_SOURCE_READS = 8
MAX_HOTSPOTS = 5
MAX_REPORT = 32_000
MAX_INPUT = 32_000
TURN_SECONDS = 120
LEASE_SECONDS = 150
CALL_SECONDS = 20
FRESH_SECONDS = 300


def load_env(path=Path(".env")):
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def safe_url(value):
    if not value:
        return value
    parts = urlsplit(value)
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("endpoint_must_not_contain_credentials_query_or_fragment")
    if parts.scheme != "https" and not (
        parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1", "::1")
    ):
        raise ValueError("https_or_loopback_required")
    if not parts.hostname:
        raise ValueError("endpoint_host_required")
    return value.rstrip("/")


@dataclass
class Config:
    data_dir: Path = field(default_factory=lambda: Path("data"))
    port: int = 8765
    git: str = "git"
    mcp_url: str = ""
    read_tools: tuple = ()
    profile_tool: str = ""
    profile_argument: str = "profile_id"
    profile_args: dict = field(default_factory=dict)
    identity_path: str = ""
    document: str = ""
    model_url: str = ""
    model_name: str = ""
    model_context: int = 16384
    executor_url: str = ""

    @classmethod
    def from_env(cls):
        load_env()
        c = cls(
            data_dir=Path(os.getenv("PA_DATA_DIR", "./data")),
            port=int(os.getenv("PA_PORT", "8765")),
            git=os.getenv("PA_GIT_EXECUTABLE", "git"),
            mcp_url=safe_url(os.getenv("PA_MCP_URL", "")),
            read_tools=tuple(
                x.strip() for x in os.getenv("PA_MCP_READ_TOOLS", "").split(",") if x.strip()
            ),
            profile_tool=os.getenv("PA_MCP_PROFILE_TOOL", ""),
            profile_argument=os.getenv("PA_MCP_PROFILE_ARGUMENT", "profile_id"),
            profile_args=json.loads(os.getenv("PA_MCP_PROFILE_ARGS", "{}")),
            identity_path=os.getenv("PA_MCP_PROFILE_IDENTITY_PATH", ""),
            document=os.getenv("PA_DOCUMENT_PATH", ""),
            model_url=safe_url(os.getenv("PA_MODEL_URL", "")),
            model_name=os.getenv("PA_MODEL_NAME", ""),
            model_context=int(os.getenv("PA_MODEL_CONTEXT_TOKENS", "16384")),
            executor_url=safe_url(os.getenv("PA_EXECUTOR_URL", "")),
        )
        if not isinstance(c.profile_args, dict) or len(json.dumps(c.profile_args)) > 8000:
            raise ValueError("invalid_profile_arguments")
        if c.profile_tool and c.profile_tool not in c.read_tools:
            raise ValueError("profile_tool_not_allowed")
        if not 1024 <= c.port <= 65535 or c.model_context < 4096:
            raise ValueError("invalid_runtime_bounds")
        return c

    def policy(self):
        return {
            k: getattr(self, k)
            for k in (
                "mcp_url",
                "read_tools",
                "profile_tool",
                "profile_argument",
                "profile_args",
                "identity_path",
                "document",
                "model_url",
                "model_name",
                "executor_url",
            )
        }
