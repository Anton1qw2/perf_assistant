import pytest
from cryptography.fernet import Fernet

from performance_assistant.config import Config
from performance_assistant.secrets import Vault
from performance_assistant.service import Service
from performance_assistant.storage import Store
from performance_assistant.worker import Worker


@pytest.fixture
def system(tmp_path, monkeypatch):
    monkeypatch.setenv("PA_MASTER_KEY", Fernet.generate_key().decode())
    config = Config(data_dir=tmp_path)
    store = Store(tmp_path)
    vault = Vault(store)
    service = Service(store, config, vault)
    worker = Worker(store, config, vault)
    cid = store.create_conversation()
    return config, store, vault, service, worker, cid


def drain(worker, limit=40):
    for _ in range(limit):
        if not worker.tick():
            return
    raise AssertionError("worker did not quiesce")
