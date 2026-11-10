"""Secure, read-only OPC UA link between the plant and Guardian.

Client side (what would run beside a real DCS / historian)
  * SignAndEncrypt only (Aes256Sha256RsaPss or Basic256Sha256); a server that
    offers no security is refused
  * mutual certificate authentication: the client pins the server certificate
  * read-only by construction: the client class exposes reads, nothing else

Server side (a demo stand-in for the DCS, publishing the simulated plant)
  * accepts only encrypted, signed sessions
  * identifies clients by certificate; unknown certificates get no access
  * a read-only permission ruleset, so even a client that tries to write is
    refused by the server: defence in depth, not just a polite client
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from asyncua import Client, Server, ua
from asyncua.crypto import cert_gen, permission_rules
from asyncua.crypto.permission_rules import PermissionRuleset, User, UserRole
from asyncua.crypto.security_policies import (SecurityPolicyAes256Sha256RsaPss,
                                              SecurityPolicyBasic256Sha256)
from asyncua.server.user_managers import CertificateUserManager
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import ExtendedKeyUsageOID

NAMESPACE = "urn:industrial-ai-guardian:te"
SECURE_POLICIES = [
    ua.SecurityPolicyType.Aes256Sha256RsaPss_SignAndEncrypt,
    ua.SecurityPolicyType.Basic256Sha256_SignAndEncrypt,
]


# --------------------------------------------------------------------------
# Certificates
# --------------------------------------------------------------------------
@dataclass
class Identity:
    cert: Path  # DER certificate
    key: Path  # PEM private key
    uri: str  # application URI (must match the certificate)


def make_identity(out_dir: str | Path, name: str, server: bool, days: int = 365) -> Identity:
    """Self-signed application certificate for a client or server.

    In a plant these would come from the site's certificate authority (or the
    OPC UA Global Discovery Server), not be self-signed.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    uri = f"urn:industrial-ai-guardian:{name}"
    key = cert_gen.generate_private_key()
    cert = cert_gen.generate_self_signed_app_certificate(
        key, name, {"organizationName": "Industrial AI Guardian"},
        [x509.UniformResourceIdentifier(uri), x509.DNSName("localhost")],
        [ExtendedKeyUsageOID.SERVER_AUTH if server else ExtendedKeyUsageOID.CLIENT_AUTH],
        days=days,
    )
    cert_path, key_path = out / f"{name}_cert.der", out / f"{name}_key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.DER))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    key_path.chmod(0o600)
    return Identity(cert_path, key_path, uri)


# --------------------------------------------------------------------------
# Server: read-only demo stand-in for the DCS
# --------------------------------------------------------------------------
READ_ONLY_TYPES = [t for t in permission_rules.USER_TYPES if t not in (
    ua.ObjectIds.WriteRequest_Encoding_DefaultBinary,
    ua.ObjectIds.CallRequest_Encoding_DefaultBinary,
)]


class ReadOnlyRuleset(PermissionRuleset):
    """Monitoring clients may browse, read and subscribe; never write or call."""

    def __init__(self):
        self._allowed = {UserRole.User: set(map(ua.NodeId, READ_ONLY_TYPES))}

    def check_validity(self, user: User, action_type_id, body) -> bool:
        return action_type_id in self._allowed.get(user.role, set())


class PlantServer:
    def __init__(self, identity: Identity, trusted_clients: list[Path], tags: list[str],
                 endpoint: str = "opc.tcp://127.0.0.1:4840/guardian/"):
        self.identity, self.trusted, self.tags, self.endpoint = identity, trusted_clients, tags, endpoint
        self.nodes: dict[str, object] = {}

    async def start(self) -> None:
        users = CertificateUserManager()
        for n, c in enumerate(self.trusted):
            await users.add_user(c, name=f"monitor{n}")
        self.server = Server(user_manager=users)
        await self.server.init()
        self.server.set_endpoint(self.endpoint)
        self.server.set_server_name("TE plant (demo DCS)")
        await self.server.set_application_uri(self.identity.uri)
        self.server.set_security_policy(SECURE_POLICIES, permission_ruleset=ReadOnlyRuleset())
        await self.server.load_certificate(str(self.identity.cert))
        await self.server.load_private_key(str(self.identity.key))
        idx = await self.server.register_namespace(NAMESPACE)
        folder = await self.server.nodes.objects.add_folder(idx, "TE")
        for t in ["time", *self.tags]:
            self.nodes[t] = await folder.add_variable(ua.NodeId(t, idx), t, 0.0)
        await self.server.start()

    async def publish(self, row: pd.Series) -> None:
        for t, node in self.nodes.items():
            await node.write_value(float(row[t]))  # server-side update, not via a client

    async def stop(self) -> None:
        await self.server.stop()


# --------------------------------------------------------------------------
# Client: what Guardian uses
# --------------------------------------------------------------------------
class InsecureEndpointError(RuntimeError):
    pass


class SecureReadOnlyClient:
    """Reads tag snapshots over an encrypted, mutually authenticated session.

    There is deliberately no write method. The server's permission ruleset
    enforces the same thing independently.
    """

    def __init__(self, endpoint: str, identity: Identity, server_cert: Path, tags: list[str]):
        self.endpoint, self.identity, self.server_cert, self.tags = endpoint, identity, server_cert, tags

    async def __aenter__(self) -> "SecureReadOnlyClient":
        self._client = Client(self.endpoint, timeout=10)
        self._client.application_uri = self.identity.uri
        await self._client.set_security(
            SecurityPolicyAes256Sha256RsaPss, str(self.identity.cert), str(self.identity.key),
            server_certificate=str(self.server_cert), mode=ua.MessageSecurityMode.SignAndEncrypt)
        await self._client.connect()
        if self._client.security_policy.Mode != ua.MessageSecurityMode.SignAndEncrypt:
            await self._client.disconnect()
            raise InsecureEndpointError("session is not signed and encrypted")
        idx = await self._client.get_namespace_index(NAMESPACE)
        self._nodes = {t: self._client.get_node(ua.NodeId(t, idx)) for t in ["time", *self.tags]}
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.disconnect()

    async def read_snapshot(self) -> dict[str, float]:
        names = list(self._nodes)
        values = await self._client.read_values([self._nodes[n] for n in names])
        return dict(zip(names, map(float, values)))


__all__ = ["make_identity", "PlantServer", "SecureReadOnlyClient", "ReadOnlyRuleset",
           "SecurityPolicyBasic256Sha256", "asyncio"]
