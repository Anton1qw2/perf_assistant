"""Secrets never belong in task/history/evidence. DPAPI on Windows; external key elsewhere."""

import ctypes
import os
from urllib.parse import urlsplit

from cryptography.fernet import Fernet

from .storage import uid


def dpapi(data, decrypt=False):
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    crypt = ctypes.windll.crypt32
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise ValueError("credential_storage_failed")
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        ctypes.windll.kernel32.LocalFree(output.data)


class Vault:
    def __init__(self, store):
        self.store = store

    def crypt(self, data, decrypt=False):
        key = os.getenv("PA_MASTER_KEY")
        if os.name == "nt" and not key:
            return dpapi(data, decrypt)
        if not key:
            raise ValueError("master_key_required")
        f = Fernet(key.encode())
        return f.decrypt(data) if decrypt else f.encrypt(data)

    def save(self, db, token, destination, repository=None):
        if urlsplit(destination).scheme != "https":
            raise ValueError("authenticated_https_required")
        if not token or len(token) > 8192 or "\n" in token or "\r" in token:
            raise ValueError("invalid_credential")
        ref = uid()
        db.execute(
            "INSERT INTO credentials VALUES(?,?,?,?,0)",
            (ref, destination, repository, self.crypt(token.encode())),
        )
        return ref

    def resolve(self, ref, destination, repository=None):
        if not ref:
            return None
        row = self.store.one("SELECT * FROM credentials WHERE id=?", (ref,))
        if (
            not row
            or row["retired"]
            or row["destination"] != destination
            or row["repository"] != repository
        ):
            raise ValueError("credential_binding_mismatch")
        return self.crypt(row["ciphertext"], True).decode()

    def scrub(self, text):
        # Also scrub retired values: old messages/errors must not resurrect credentials.
        for row in self.store.rows("SELECT ciphertext FROM credentials"):
            token = self.crypt(row["ciphertext"], True).decode()
            text = text.replace(token, "[credential removed]")
        return text

    def import_environment(self, config):
        for name, destination in [
            ("MCP", config.mcp_url),
            ("MODEL", config.model_url),
            ("EXECUTOR", config.executor_url),
        ]:
            token = os.environ.get(f"PA_{name}_TOKEN", "")
            if not token or not destination:
                continue
            with self.store.tx() as db:
                key = f"{name.lower()}_credential"
                old = db.execute("SELECT value FROM app_meta WHERE key=?", (key,)).fetchone()
                if old:
                    row = db.execute(
                        "SELECT * FROM credentials WHERE id=?", (old["value"],)
                    ).fetchone()
                    if (
                        row
                        and row["destination"] == destination
                        and self.crypt(row["ciphertext"], True).decode() == token
                    ):
                        continue
                ref = self.save(db, token, destination)
                db.execute("INSERT OR REPLACE INTO app_meta VALUES(?,?)", (key, ref))

    def default_ref(self, source):
        row = self.store.one("SELECT value FROM app_meta WHERE key=?", (f"{source}_credential",))
        return row["value"] if row else None
