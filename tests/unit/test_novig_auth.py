import base64
import json
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519

from news_edge.market import novig_auth

# Novig's published signing vectors and test keypairs (labelled not for production use).
VECTORS = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "novig_signing_vectors.json").read_text()
)


def _key(keypair_id: str) -> novig_auth.PrivateKey:
    pem = VECTORS["keypairs"][keypair_id]["private_key_pkcs8_pem"].encode()
    key = serialization.load_pem_private_key(pem, password=None)
    assert isinstance(key, ed25519.Ed25519PrivateKey | ec.EllipticCurvePrivateKey)
    return key


def _string(vector: dict[str, Any]) -> str:
    i = vector["input"]
    return novig_auth.string_to_sign(
        i["timestamp"], i["method"], i["path"], i["query"], i["body"].encode()
    )


@pytest.mark.parametrize("vector", VECTORS["vectors"], ids=lambda v: v["id"])
def test_string_to_sign_matches_vector(vector: dict[str, Any]) -> None:
    assert _string(vector) == vector["string_to_sign"]


@pytest.mark.parametrize(
    "vector",
    [v for v in VECTORS["vectors"] if v["algorithm"] == "ed25519"],
    ids=lambda v: v["id"],
)
def test_ed25519_signature_matches_vector(vector: dict[str, Any]) -> None:
    assert novig_auth.sign(_key(vector["keypair_id"]), _string(vector)) == vector["signature"]


def test_p256_signature_is_der_and_verifies() -> None:
    vector = next(v for v in VECTORS["vectors"] if v["algorithm"] == "ecdsa-p256")
    key = _key(vector["keypair_id"])
    assert isinstance(key, ec.EllipticCurvePrivateKey)
    sig = base64.b64decode(novig_auth.sign(key, _string(vector)))
    assert 70 <= len(sig) <= 72
    key.public_key().verify(sig, _string(vector).encode(), ec.ECDSA(hashes.SHA256()))


def test_signer_headers(tmp_path: Path) -> None:
    pem = tmp_path / "k.pem"
    pem.write_bytes(novig_auth.private_key_pem(_key("ed25519-test-1")))
    signer = novig_auth.Signer.from_pem("key-1", pem)
    headers = signer.headers("get", "/v3/ws", timestamp_ms=1755000000000)
    assert headers["Novig-Key-Id"] == "key-1"
    assert headers["Novig-Timestamp"] == "1755000000000"
    expected = novig_auth.string_to_sign(1755000000000, "GET", "/v3/ws", "", b"")
    raw = base64.b64decode(headers["Novig-Signature"])
    ed = _key("ed25519-test-1")
    assert isinstance(ed, ed25519.Ed25519PrivateKey)
    ed.public_key().verify(raw, expected.encode())


def test_public_key_pem_matches_published() -> None:
    pub = novig_auth.public_key_pem(_key("ed25519-test-1"))
    assert pub == VECTORS["keypairs"]["ed25519-test-1"]["public_key_spki_pem"]


def test_rejects_rsa_key(tmp_path: Path) -> None:
    from cryptography.hazmat.primitives.asymmetric import rsa

    pem = tmp_path / "rsa.pem"
    pem.write_bytes(
        rsa.generate_private_key(65537, 2048).private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    with pytest.raises(ValueError, match="Ed25519 or P-256"):
        novig_auth.load_private_key(pem)
