"""Portable line TUI. HTTP only; deliberately no SQLite or agent imports."""

import json
import uuid

import httpx


def run(port):
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10, trust_env=False) as client:
        conversations = client.get("/api/conversations").json()
        if not conversations:
            conversations = [client.post("/api/conversations", json={}).json()]
        cid = conversations[0]["id"]
        print(
            "Performance Assistant · /refresh /list /select ID /new /task /ask /check /run /cancel /quit"
        )
        print('Set targets: /target {"profile_id":"result-id"} or {"repository":"/path"}')
        while True:
            try:
                snapshot = client.get(f"/api/conversations/{cid}").json()
                statuses = []
                for source in ("profiler", "git", "docs"):
                    check = snapshot["checks"].get(source)
                    state = (
                        "unchecked"
                        if not check
                        else "stale"
                        if check["stale"]
                        else ",".join(f"{k}={v['outcome']}" for k, v in check["stages"].items())
                    )
                    statuses.append(source + ": " + state)
                print("\n" + " | ".join(statuses))
                for m in snapshot["messages"][-6:]:
                    # No terminal control sequences from untrusted provider content.
                    text = "".join(
                        c for c in m["text"] if c in "\n\t" or ord(c) >= 32 and ord(c) != 127
                    )
                    print(f"\n{m['role']}: {text}")
                for r in snapshot["runs"][:1]:
                    print("Run:", r["status"], r["checkpoint"]["phase"])
                    for q in r["questions"]:
                        if q["answer"] is None:
                            print(
                                "Question:", json.dumps(q["payload"]["prompt"], ensure_ascii=False)
                            )
                text = input("\n> ").strip()
                if text == "/quit":
                    return
                if text == "/refresh" or not text:
                    continue
                if text == "/list":
                    print(client.get("/api/conversations").json())
                    continue
                if text == "/new":
                    cid = client.post("/api/conversations", json={}).json()["id"]
                    continue
                if text.startswith("/select "):
                    target = text.split(" ", 1)[1]
                    if client.get("/api/conversations/" + target).status_code == 200:
                        cid = target
                    continue
                body = {"request_id": str(uuid.uuid4())}
                if text.startswith("/target "):
                    values = json.loads(text.split(" ", 1)[1])
                    response = client.patch(
                        f"/api/conversations/{cid}/task",
                        json={
                            **body,
                            "patch": {
                                "expected_version": snapshot["task"]["spec_version"],
                                **values,
                            },
                        },
                    )
                else:
                    response = client.post(
                        f"/api/conversations/{cid}/messages", json={**body, "text": text}
                    )
                if response.is_error:
                    print(response.json().get("code", "request_failed"))
            except (KeyboardInterrupt, EOFError):
                return
            except (httpx.HTTPError, ValueError, KeyError):
                print("Request failed. Check API and command syntax.")
