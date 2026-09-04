from __future__ import annotations

from dataclasses import dataclass

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


@dataclass(frozen=True, slots=True)
class SSHCredentials:
    client_private: str
    client_public: str
    host_private: str
    host_public: str


def generate_ssh_credentials() -> SSHCredentials:
    client = Ed25519PrivateKey.generate()
    host = Ed25519PrivateKey.generate()

    def private(key: Ed25519PrivateKey) -> str:
        return key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        ).decode()

    def public(key: Ed25519PrivateKey) -> str:
        return key.public_key().public_bytes(
            serialization.Encoding.OpenSSH,
            serialization.PublicFormat.OpenSSH,
        ).decode()

    return SSHCredentials(private(client), public(client), private(host), public(host))
