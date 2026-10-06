"""Sign Novig API requests with NOVIG-V3.

Every request, the websocket upgrade included, carries three headers: the key ID, a
Unix-millisecond timestamp, and a signature over a six-line canonical string.

Spec and test vectors: https://docs.novig.com/api/signing.md
"""

import base64
import hashlib
import time
from pathlib import Path
from typing import Literal
from urllib.parse import quote, unquote_to_bytes

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519

SCHEME = "NOVIG-V3"

Environment = Literal["paper", "production"]
HOSTS: dict[Environment, str] = {
    "paper": "https://api.paper.novig.com",
    "production": "https://api.novig.com",
}

PrivateKey = ed25519.Ed25519PrivateKey | ec.EllipticCurvePrivateKey


def canonical_query(raw: str) -> str:
    """Decode each name and value (a bare + stays a +), re-encode everything but
    ALPHA DIGIT - . _ ~ as uppercase %XX, and sort by name, then value."""
    pairs = []
    for token in raw.split("&"):
        if not token:
            continue
        name, _, value = token.partition("=")
        pairs.append((_encode(name), _encode(value)))
    return "&".join(f"{name}={value}" for name, value in sorted(pairs))


def _encode(part: str) -> str:
    return quote(unquote_to_bytes(part), safe="-._~")


def string_to_sign(timestamp_ms: int, method: str, path: str, query: str, body: bytes) -> str:
    return "\n".join(
        [
            SCHEME,
            str(timestamp_ms),
            method.upper(),
            path,
            canonical_query(query),
            hashlib.sha256(body).hexdigest(),
        ]
    )


def load_private_key(path: Path) -> PrivateKey:
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, ed25519.Ed25519PrivateKey | ec.EllipticCurvePrivateKey):
        raise ValueError(f"{path}: Novig keys are Ed25519 or P-256, got {type(key).__name__}")
    return key


def sign(key: PrivateKey, message: str) -> str:
    """Standard padded base64 of the signature. P-256 signatures are DER, as Novig expects."""
    data = message.encode()
    if isinstance(key, ed25519.Ed25519PrivateKey):
        raw = key.sign(data)
    else:
        raw = key.sign(data, ec.ECDSA(hashes.SHA256()))
    return base64.b64encode(raw).decode()


class Signer:
    """Builds the three auth headers for one key."""

    def __init__(self, key_id: str, key: PrivateKey) -> None:
        self.key_id = key_id
        self._key = key

    @classmethod
    def from_pem(cls, key_id: str, pem_path: Path) -> "Signer":
        return cls(key_id, load_private_key(pem_path))

    def headers(
        self,
        method: str,
        path: str,
        query: str = "",
        body: bytes = b"",
        timestamp_ms: int | None = None,
    ) -> dict[str, str]:
        ts = timestamp_ms if timestamp_ms is not None else time.time_ns() // 1_000_000
        return {
            "Novig-Key-Id": self.key_id,
            "Novig-Timestamp": str(ts),
            "Novig-Signature": sign(self._key, string_to_sign(ts, method, path, query, body)),
        }


def public_key_pem(key: PrivateKey) -> str:
    """The SPKI PEM Novig's key routes take as publicKey."""
    return (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )


def private_key_pem(key: PrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
