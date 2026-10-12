import pytest


@pytest.fixture(autouse=True)
def isolate_local_adapter(monkeypatch):
    # Tests must not depend on the developer's physical network address.
    monkeypatch.delenv('LLM_LOCAL_ADDRESS', raising=False)
