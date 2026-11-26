"""Keep every test's per-phone ownership locks out of the owner's real state directory."""

import pytest


@pytest.fixture(autouse=True)
def private_phone_locks(tmp_path_factory, monkeypatch):
    directory = tmp_path_factory.mktemp("phone-locks")
    monkeypatch.setenv("IPHONE_AGENT_LOCK_DIR", str(directory))
    return directory
