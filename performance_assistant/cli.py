import argparse
import json
from pathlib import Path

from .config import Config
from .contracts import SCHEMAS
from .secrets import Vault
from .storage import Store, uid


def main():
    parser = argparse.ArgumentParser(description="Performance Assistant — local workbench")
    parser.add_argument("command", choices=["serve", "api", "worker", "tui", "schemas", "smoke"])
    parser.add_argument("--check", action="store_true", help="Check schema drift")
    args = parser.parse_args()
    if args.command == "schemas":
        folder = Path("schemas")
        folder.mkdir(exist_ok=True)
        drift = []
        for model in SCHEMAS:
            path = folder / (model.__name__ + ".json")
            content = json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2) + "\n"
            if args.check:
                if not path.exists() or path.read_text(encoding="utf-8") != content:
                    drift.append(model.__name__)
            else:
                path.write_text(content, encoding="utf-8")
        if drift:
            raise SystemExit("Schema drift: " + ", ".join(drift))
        print("Schemas match." if args.check else "Schemas exported.")
        return
    config = Config.from_env()
    if args.command == "serve":
        import os
        import subprocess
        import sys
        import time

        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        processes = [
            subprocess.Popen(
                [sys.executable, "-m", "performance_assistant", name], creationflags=flags
            )
            for name in ("api", "worker")
        ]
        print(
            f"Performance Assistant: http://127.0.0.1:{config.port} — Ctrl+C to stop both processes."
        )
        try:
            while all(p.poll() is None for p in processes):
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
            for process in processes:
                process.wait(timeout=10)
    elif args.command == "api":
        import uvicorn

        from .api import create_app

        uvicorn.run(create_app(config), host="127.0.0.1", port=config.port, access_log=False)
    elif args.command == "worker":
        from .worker import Worker

        store = Store(config.data_dir)
        vault = Vault(store)
        vault.import_environment(config)
        print("Worker running. Ctrl+C to stop.")
        try:
            Worker(store, config, vault).serve()
        except KeyboardInterrupt:
            pass
    elif args.command == "smoke":
        import httpx

        # Explicitly requested CLI smoke performs real diagnostics through the HTTP API.
        with httpx.Client(base_url=f"http://127.0.0.1:{config.port}", trust_env=False) as client:
            print(client.get("/api/health").json())
            cid = client.post("/api/conversations", json={}).json()["id"]
            result = client.post(
                f"/api/conversations/{cid}/connection-checks",
                json={"request_id": uid(), "expected_settings_revision": 0},
            ).json()
            print(json.dumps({"conversation_id": cid, "queued": result}, ensure_ascii=False))
    else:
        from .tui import run

        run(config.port)


if __name__ == "__main__":
    main()
