"""Security and command handling of the live dashboard server.

A stand-in session replaces the simulator so these tests run in milliseconds.
"""

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from dashboard.engine import CommandError  # noqa: E402
from dashboard.server import create_app  # noqa: E402
from guardian.audit import AuditLog  # noqa: E402

ORIGIN = {"origin": "http://127.0.0.1:8050", "host": "127.0.0.1:8050"}


class FakeSession:
    def __init__(self, audit):
        self.audit, self.trip, self.calls = audit, None, []
        self.state = {"t": 5.0, "trip": None, "guardian": {}, "alarms": [], "truth": []}

    def snapshot(self):
        return {"series": [], "state": self.state}

    def tick(self):
        return self.state

    def set_setpoint(self, name, value):
        if value > 100:
            raise CommandError("out of range")
        self.calls.append(("sp", name, value))

    def inject_fault(self, tag, kind, magnitude):
        self.calls.append(("fault", tag, kind, magnitude))

    def stop(self):
        pass


@pytest.fixture
def client(tmp_path):
    path = tmp_path / "audit.jsonl"
    session = FakeSession(AuditLog(path, key=b"k"))
    app = create_app(session, "secret-token", path, b"k")
    with TestClient(app, base_url="http://127.0.0.1:8050") as c:
        c.session = session
        yield c


def test_page_has_strict_security_headers(client):
    r = client.get("/")
    assert r.status_code == 200
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "unsafe" not in csp
    assert r.headers["x-content-type-options"] == "nosniff"
    assert client.get("/docs").status_code == 404  # no API explorer


def test_websocket_refuses_wrong_token(client):
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect("/ws?token=wrong", headers=ORIGIN) as ws:
            ws.receive_text()
    assert e.value.code == 1008


def test_websocket_refuses_foreign_origin(client):
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect("/ws?token=secret-token", headers={"origin": "http://evil.example", "host": "127.0.0.1:8050"}) as ws:
            ws.receive_text()
    assert e.value.code == 1008


def _hello(ws):
    msg = ws.receive_json()
    while msg["type"] != "hello":
        msg = ws.receive_json()
    return msg


def _result(ws):
    msg = ws.receive_json()
    while msg["type"] != "result":
        msg = ws.receive_json()
    return msg


def test_valid_command_reaches_the_plant(client):
    with client.websocket_connect("/ws?token=secret-token", headers=ORIGIN) as ws:
        assert "meta" in _hello(ws)
        ws.send_json({"cmd": "fault", "tag": "xmeas_9", "kind": "bias", "magnitude": 1.0, "req": 1})
        r = _result(ws)
        assert r["ok"] and r["req"] == 1
    assert client.session.calls == [("fault", "xmeas_9", "bias", 1.0)]


@pytest.mark.parametrize("bad", [
    {"cmd": "fault", "tag": "xmv_10", "kind": "bias", "magnitude": 1},       # valves are not field transmitters
    {"cmd": "fault", "tag": "xmeas_9", "kind": "melt", "magnitude": 1},      # unknown fault type
    {"cmd": "disturbance", "idv": 99, "on": True},                            # out of range
    {"cmd": "shell", "line": "rm -rf /"},                                     # unknown command
    {"cmd": "setpoint", "name": "prod_rate", "value": 1e6},                   # rejected by the plant layer
    {"cmd": "fault", "tag": "xmeas_9", "kind": "bias", "magnitude": 1, "extra": 1},  # unexpected field
    {"cmd": "fault", "tag": "xmeas_9\nsfclear all", "kind": "bias", "magnitude": 1},  # line injection
    {"cmd": "setpoint", "name": "prod_rate", "value": "23"},                  # wrong type
    {"cmd": "reset", "seed": -5},                                             # out of range
    [1, 2, 3],                                                                # not an object
])
def test_invalid_commands_are_rejected(client, bad):
    with client.websocket_connect("/ws?token=secret-token", headers=ORIGIN) as ws:
        _hello(ws)
        ws.send_json(bad)
        assert _result(ws)["ok"] is False
    assert client.session.calls == []


def test_oversized_messages_are_rejected(client):
    with client.websocket_connect("/ws?token=secret-token", headers=ORIGIN) as ws:
        _hello(ws)
        ws.send_text('{"cmd":"run","running":true,"pad":"' + "x" * 5000 + '"}')
        assert _result(ws)["ok"] is False


@pytest.mark.parametrize("payload", [
    '{"cmd":"fault","tag":"xmeas_9","kind":"bias","magnitude":NaN}',
    '{"cmd":"fault","tag":"xmeas_9","kind":"noise","magnitude":Infinity}',
    '{"cmd":"setpoint","name":"prod_rate","value":-Infinity}',
])
def test_non_finite_numbers_are_rejected(client, payload):
    with client.websocket_connect("/ws?token=secret-token", headers=ORIGIN) as ws:
        _hello(ws)
        ws.send_text(payload)
        assert _result(ws)["ok"] is False
    assert client.session.calls == []


def test_dns_rebinding_host_is_refused(client):
    r = client.get("/", headers={"host": "attacker.example:8050"})
    assert r.status_code == 400


def test_server_keeps_running_when_the_plant_fails(tmp_path):
    path = tmp_path / "audit.jsonl"
    session = FakeSession(AuditLog(path, key=b"k"))

    connected = []
    real_snapshot = session.snapshot

    def snapshot():
        connected.append(True)
        return real_snapshot()

    def broken():
        if connected:  # fail only once a client is there to be told
            raise RuntimeError("simulator stopped unexpectedly")
        return session.state

    session.snapshot, session.tick = snapshot, broken
    app = create_app(session, "secret-token", path, b"k")
    with TestClient(app, base_url="http://127.0.0.1:8050") as c:
        with c.websocket_connect("/ws?token=secret-token", headers=ORIGIN) as ws:
            _hello(ws)
            msg = _result(ws)
            assert msg["ok"] is False and "paused" in msg["message"]
            assert msg["control"]["running"] is False
        assert c.get("/").status_code == 200


def test_ground_truth_stays_on_the_server_until_revealed(client):
    truth = [{"kind": "sensor_fault", "label": "TT bias", "t": 4.0, "active": True},
             {"kind": "disturbance", "label": "IDV(1)", "t": 4.5, "active": True, "idv": 1}]
    point = {"t": 5.0, "meas": {"xmeas_9": 120.4}, "true": {"xmeas_9": 120.4}}
    client.session.state = {**client.session.state, "truth": truth, "point": point}
    with client.websocket_connect("/ws?token=secret-token", headers=ORIGIN) as ws:
        hello = _hello(ws)
        assert hello["control"]["reveal"] is False
        assert "true" not in hello["state"]["point"]
        assert [e["kind"] for e in hello["state"]["truth"]] == ["disturbance"]

        ws.send_json({"cmd": "reveal", "on": True, "req": 2})
        snap = ws.receive_json()
        while snap["type"] != "snapshot":
            snap = ws.receive_json()
        assert snap["state"]["point"]["true"] == {"xmeas_9": 120.4}
        assert len(snap["state"]["truth"]) == 2
        assert _result(ws)["control"]["reveal"] is True
    assert any(r["event"].get("action") == "ground_truth_revealed" for r in client.session.audit.tail(10))
