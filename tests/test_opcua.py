"""End-to-end checks of the secure OPC UA link (runs on localhost)."""

import asyncio
import socket

import pandas as pd
import pytest

pytest.importorskip("asyncua")

from asyncua import Client, ua  # noqa: E402

from guardian.opcua import (PlantServer, SecureReadOnlyClient,  # noqa: E402
                            make_identity)

TAGS = ["xmeas_9", "xmeas_21", "xmv_10"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def pki(tmp_path):
    return {
        "server": make_identity(tmp_path, "plant", server=True),
        "guardian": make_identity(tmp_path, "guardian", server=False),
        "intruder": make_identity(tmp_path, "intruder", server=False),
    }


def _with_server(pki, body):
    async def main():
        ep = f"opc.tcp://127.0.0.1:{_free_port()}/guardian/"
        srv = PlantServer(pki["server"], [pki["guardian"].cert], TAGS, ep)
        await srv.start()
        try:
            await srv.publish(pd.Series({"time": 12.5, "xmeas_9": 122.9, "xmeas_21": 102.4, "xmv_10": 36.1}))
            return await body(ep)
        finally:
            await srv.stop()
    return asyncio.run(main())


def test_secure_client_reads_plant_values(pki):
    async def body(ep):
        async with SecureReadOnlyClient(ep, pki["guardian"], pki["server"].cert, TAGS) as c:
            return await c.read_snapshot()
    snap = _with_server(pki, body)
    assert snap == {"time": 12.5, "xmeas_9": 122.9, "xmeas_21": 102.4, "xmv_10": 36.1}


def test_server_refuses_unencrypted_sessions(pki):
    async def body(ep):
        c = Client(ep, timeout=5)
        with pytest.raises(Exception):
            await c.connect()  # no security policy offered -> refused
    _with_server(pki, body)


def test_server_refuses_unknown_certificates(pki):
    async def body(ep):
        with pytest.raises(Exception):
            async with SecureReadOnlyClient(ep, pki["intruder"], pki["server"].cert, TAGS) as c:
                await c.read_snapshot()
    _with_server(pki, body)


def test_server_refuses_writes_even_from_a_trusted_client(pki):
    async def body(ep):
        async with SecureReadOnlyClient(ep, pki["guardian"], pki["server"].cert, TAGS) as c:
            node = c._nodes["xmv_10"]  # bypass the read-only wrapper on purpose
            with pytest.raises(ua.UaStatusCodeError):
                await node.write_value(ua.DataValue(ua.Variant(0.0, ua.VariantType.Double)))
            return await c.read_snapshot()
    assert _with_server(pki, body)["xmv_10"] == 36.1
