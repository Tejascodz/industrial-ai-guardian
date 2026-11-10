"""Live dashboard server: FastAPI + WebSocket, local-only by default.

Security of the dashboard itself
  * binds to 127.0.0.1 and only answers requests addressed to 127.0.0.1 or
    localhost (blocks DNS-rebinding pages); a random access token is required for the WebSocket
    that carries data and trainer commands (printed once at start-up)
  * the WebSocket checks the Origin header (no cross-site WebSocket hijacking)
  * every command is validated (types, ranges) before it reaches the plant,
    and commands are rate-limited per connection
  * strict Content-Security-Policy: only this server's own scripts, styles and
    fonts; no inline script, no third-party CDN, no framing
  * all operator and trainer actions go to the HMAC-chained audit log
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal, Union

import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from guardian.audit import AuditLog

from . import plant
from .engine import CommandError, LiveSession

ROOT = Path(__file__).resolve().parents[1]
STATIC = Path(__file__).resolve().parent / "static"
SPEEDS = [0.5, 1.0, 2.0, 4.0, 8.0]  # historian samples per second (1 sample = 3 plant minutes)


# ----- command schema (everything a client may send) --------------------------
class _Cmd(BaseModel):
    # Strict types, no unknown fields, no NaN / Infinity sneaking in through JSON floats.
    model_config = {"extra": "forbid", "allow_inf_nan": False, "strict": True}
    req: int | None = None


class Run(_Cmd):
    cmd: Literal["run"]
    running: bool


class Speed(_Cmd):
    cmd: Literal["speed"]
    value: float


class Reset(_Cmd):
    cmd: Literal["reset"]
    seed: int = Field(ge=1, le=1_000_000)


class SetpointCmd(_Cmd):
    cmd: Literal["setpoint"]
    name: str = Field(max_length=32)
    value: float


class FaultCmd(_Cmd):
    cmd: Literal["fault"]
    tag: str = Field(pattern=r"^xmeas_\d{1,2}$")
    kind: Literal["bias", "drift", "stuck", "noise"]
    magnitude: float = 0.0


class ClearFaults(_Cmd):
    cmd: Literal["clear_faults"]


class DisturbanceCmd(_Cmd):
    cmd: Literal["disturbance"]
    idv: int = Field(ge=1, le=20)
    on: bool


class AttackCmd(_Cmd):
    cmd: Literal["attack"]
    preset: str = Field(max_length=40)
    jitter: bool = False


class StopAttacks(_Cmd):
    cmd: Literal["stop_attacks"]


class Ack(_Cmd):
    cmd: Literal["ack"]
    id: int | None = None


class AuditCmd(_Cmd):
    cmd: Literal["audit"]


class RevealCmd(_Cmd):
    cmd: Literal["reveal"]
    on: bool


Command = Annotated[Union[Run, Speed, Reset, SetpointCmd, FaultCmd, ClearFaults, DisturbanceCmd,
                          AttackCmd, StopAttacks, Ack, AuditCmd, RevealCmd], Field(discriminator="cmd")]
COMMAND = TypeAdapter(Command)


def _jsonable(obj):
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (np.floating, float)):
        v = float(obj)
        return v if np.isfinite(v) else None
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    return obj


def dumps(obj) -> str:
    return json.dumps(_jsonable(obj), allow_nan=False, separators=(",", ":"))


class Hub:
    """Runs the plant clock and fans state out to connected clients."""

    def __init__(self, session: LiveSession, audit_path: Path, key: bytes):
        self.session, self.audit_path, self.key = session, audit_path, key
        self.clients: set[WebSocket] = set()
        self.running = True
        self.speed = 1.0
        # Ground truth (true sensor values, what was injected) never leaves the
        # server until the trainer reveals it, so a blind demo stays blind even
        # to someone reading the WebSocket traffic.
        self.reveal = False

    # ----- what clients may see -------------------------------------------------
    def view_point(self, p: dict | None) -> dict | None:
        if p is None or self.reveal:
            return p
        return {k: v for k, v in p.items() if k != "true"}

    def view_state(self, state: dict | None) -> dict | None:
        if state is None:
            return None
        out = dict(state)
        out["point"] = self.view_point(state.get("point"))
        if not self.reveal:
            # Disturbances stay listed: the trainer needs them to stop one, and a
            # process disturbance is not something Guardian is meant to name.
            out["truth"] = [e for e in state.get("truth") or [] if e.get("kind") == "disturbance"]
        return out

    def view_snapshot(self) -> dict:
        snap = self.session.snapshot()
        return {"series": [self.view_point(p) for p in snap["series"]],
                "state": self.view_state(snap["state"])}

    async def loop(self) -> None:
        while True:
            t0 = time.perf_counter()
            if self.running and not self.session.trip:
                try:
                    state = await asyncio.to_thread(self.session.tick)
                    await self.broadcast({"type": "tick", "state": self.view_state(state)})
                except Exception as e:  # keep serving: pause and tell the operator
                    self.running = False
                    await self.broadcast({"type": "result", "ok": False, "control": self.control(),
                                          "message": f"Simulation paused after an error: {e}. Restart the plant."})
            await asyncio.sleep(max(0.02, 1.0 / self.speed - (time.perf_counter() - t0)))

    async def broadcast(self, msg: dict) -> None:
        text = dumps(msg)
        for ws in list(self.clients):
            try:
                await ws.send_text(text)
            except Exception:
                self.clients.discard(ws)

    def control(self) -> dict:
        return {"running": self.running, "speed": self.speed, "speeds": SPEEDS, "reveal": self.reveal}

    async def handle(self, c) -> str:
        s = self.session
        if isinstance(c, Run):
            self.running = c.running
            s.audit.append({"action": "run" if c.running else "pause"})
            return "Running" if c.running else "Paused"
        if isinstance(c, Speed):
            if c.value not in SPEEDS:
                raise CommandError("unsupported speed")
            self.speed = c.value
            return f"Speed set to {int(c.value * 180)}× real time"
        if isinstance(c, Reset):
            await asyncio.to_thread(s.start, c.seed)
            await self.broadcast({"type": "snapshot", **self.view_snapshot(), "control": self.control()})
            return f"Plant restarted with seed {c.seed}"
        if isinstance(c, SetpointCmd):
            await asyncio.to_thread(s.set_setpoint, c.name, c.value)
            return "Setpoint changed"
        if isinstance(c, FaultCmd):
            await asyncio.to_thread(s.inject_fault, c.tag, c.kind, c.magnitude)
            return "Fault injected"
        if isinstance(c, ClearFaults):
            await asyncio.to_thread(s.clear_faults)
            return "Faults cleared"
        if isinstance(c, DisturbanceCmd):
            await asyncio.to_thread(s.disturbance, c.idv, c.on)
            return "Disturbance started" if c.on else "Disturbance stopped"
        if isinstance(c, AttackCmd):
            await asyncio.to_thread(s.attack, c.preset, c.jitter)
            return "Attack launched"
        if isinstance(c, StopAttacks):
            await asyncio.to_thread(s.stop_attacks)
            return "Attacks stopped"
        if isinstance(c, RevealCmd):
            if c.on != self.reveal:
                self.reveal = c.on
                s.audit.append({"action": "ground_truth_" + ("revealed" if c.on else "hidden")})
                await self.broadcast({"type": "snapshot", **self.view_snapshot(), "control": self.control()})
            return "Ground truth shown" if c.on else "Ground truth hidden"
        if isinstance(c, Ack):
            await asyncio.to_thread(s.acknowledge, c.id)
            await self.broadcast({"type": "tick", "state": self.view_state(s.state)})
            return "Acknowledged"
        raise CommandError("unsupported command")

    def audit_view(self) -> dict:
        ok, bad, records = self.session.audit.check(200)
        return {"ok": ok, "firstBad": bad, "records": records[::-1], "path": str(self.audit_path.name)}


def create_app(session: LiveSession, token: str, audit_path: Path, key: bytes,
               host: str = "127.0.0.1", port: int = 8050) -> FastAPI:
    hub = Hub(session, audit_path, key)
    origins = {f"http://{host}:{port}", f"http://localhost:{port}", f"http://127.0.0.1:{port}"}

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(hub.loop())
        yield
        task.cancel()
        session.stop()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.hub = hub
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=sorted({host, "127.0.0.1", "localhost"}))

    @app.middleware("http")
    async def security_headers(request, call_next):
        resp: Response = await call_next(request)
        resp.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; img-src 'self' data:; "
            f"connect-src 'self' ws://{host}:{port} ws://localhost:{port} ws://127.0.0.1:{port}; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        given = ws.query_params.get("token", "")
        origin = ws.headers.get("origin", "")
        if not hmac.compare_digest(given.encode(), token.encode()) or origin not in origins:
            await ws.close(code=1008)  # policy violation
            return
        await ws.accept()
        hub.clients.add(ws)
        await ws.send_text(dumps({"type": "hello", "meta": plant.meta(), **hub.view_snapshot(),
                                  "control": hub.control()}))
        bucket, last = 10.0, time.monotonic()
        try:
            while True:
                text = await ws.receive_text()
                now = time.monotonic()
                bucket = min(10.0, bucket + (now - last) * 5.0)
                last = now
                req = None
                try:
                    if len(text) > 4096:
                        raise CommandError("message too large")
                    if bucket < 1.0:
                        raise CommandError("too many commands; slow down")
                    bucket -= 1.0
                    data = json.loads(text)
                    req = data.get("req") if isinstance(data, dict) else None
                    cmd = COMMAND.validate_python(data)
                    if isinstance(cmd, AuditCmd):
                        await ws.send_text(dumps({"type": "audit", "req": req, **hub.audit_view()}))
                        continue
                    msg = await hub.handle(cmd)
                    await ws.send_text(dumps({"type": "result", "req": req, "ok": True, "message": msg,
                                              "control": hub.control()}))
                except (CommandError, ValidationError, ValueError) as e:
                    detail = e.errors()[0]["msg"] if isinstance(e, ValidationError) else str(e)
                    await ws.send_text(dumps({"type": "result", "req": req, "ok": False, "message": detail}))
        except WebSocketDisconnect:
            pass
        finally:
            hub.clients.discard(ws)

    return app


def new_token() -> str:
    return os.environ.get("GUARDIAN_DASHBOARD_TOKEN") or secrets.token_urlsafe(24)
