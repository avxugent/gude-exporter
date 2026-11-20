"""bao.get_credentials with hvac mocked out -- no OpenBao needed."""

from __future__ import annotations

import pytest

from gude_exporter import bao


class FakeClient:
    last = None

    def __init__(self, **kwargs):
        FakeClient.last = kwargs
        self.secrets = self
        self.kv = self
        self.v1 = self
        self.v2 = self
        self.paths = []

    def read_secret(self, mount_point, path):
        FakeClient.last_path = (mount_point, path)
        return {"data": {"username": "u1", "password": "p1"}}

    def read_secret_version(self, mount_point, path):
        FakeClient.last_path = (mount_point, path)
        return {"data": {"data": {"username": "u2", "password": "p2"}}}


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(bao, "BAO_ADDR", "https://bao.example")
    monkeypatch.setattr(bao, "BAO_TOKEN", "tok")
    monkeypatch.setattr(bao, "BAO_KV_MOUNT", "secret")
    monkeypatch.setattr(bao, "BAO_PATH_PREFIX", "gude")
    monkeypatch.setattr(bao, "BAO_KV_VERSION", "2")
    monkeypatch.setattr(bao.hvac, "Client", FakeClient)


def test_kv2_lookup(configured):
    assert bao.get_credentials("pdu1") == ("u2", "p2")
    assert FakeClient.last_path == ("secret", "gude/pdu1")


def test_kv1_lookup(configured, monkeypatch):
    monkeypatch.setattr(bao, "BAO_KV_VERSION", "1")
    assert bao.get_credentials("pdu1") == ("u1", "p1")


def test_empty_prefix_gives_clean_path(configured, monkeypatch):
    monkeypatch.setattr(bao, "BAO_PATH_PREFIX", "")
    bao.get_credentials("pdu1")
    assert FakeClient.last_path[1] == "pdu1"


def test_unconfigured_returns_none(monkeypatch):
    monkeypatch.setattr(bao, "BAO_ADDR", None)
    assert bao.get_credentials("pdu1") is None


def test_empty_hostname_returns_none(configured):
    assert bao.get_credentials("") is None


def test_failure_returns_none_and_never_logs_secret(configured, monkeypatch, caplog):
    def boom(**kw):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(bao.hvac, "Client", boom)
    assert bao.get_credentials("pdu1") is None
    assert "tok" not in caplog.text


def test_secret_missing_keys_returns_none(configured, monkeypatch):
    monkeypatch.setattr(FakeClient, "read_secret_version",
                        lambda self, mount_point, path: {"data": {"data": {"username": "u"}}})
    assert bao.get_credentials("pdu1") is None
