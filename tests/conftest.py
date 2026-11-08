import copy
import json
import socket
from pathlib import Path

import pytest

from debyko_ccxt import DebykoCCXT

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.delenv("DEBYKO_API_KEY", raising=False)

    def refuse(*args, **kwargs):
        raise AssertionError("Tests must not call external addresses")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


@pytest.fixture
def fixture():
    return lambda name: json.loads((FIXTURES / (name + ".json")).read_text())


@pytest.fixture
def api(monkeypatch, fixture):
    client = DebykoCCXT("fake-key")
    monkeypatch.setattr(client, "_now", lambda: 1794153600000)  # 2026-11-08 16:00 UTC
    replies = {
        "/v2/instruments": fixture("instruments"),
        "/v2/snapshots": fixture("snapshots"),
        "/v2/funding/settlements": fixture("settlements"),
        "/v2/snapshots/history": fixture("oi"),
        "/v2/liquidations": fixture("liquidations"),
    }
    calls = []

    def request(method, path, **kwargs):
        calls.append((method, path, copy.deepcopy(kwargs)))
        response = replies[path]
        return copy.deepcopy(response(method, path, **kwargs) if callable(response) else response)

    monkeypatch.setattr(client._rest, "request", request)
    return client, replies, calls
