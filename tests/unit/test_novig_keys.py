import base64
import importlib.util
import json
import stat
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from news_edge.market import novig_auth, novig_keys

SCRIPT = Path(__file__).parents[2] / "scripts" / "create_read_key.py"
MGMT_ID = "2c9a7e1d-5b3f-4e8a-a6d0-9f1b3c5e7a24"
SUB_ID = "b7c3a1e9-4d52-4a1f-9c8e-6f0a2b3d5e71"
READ_ID = "8f14e45f-ceea-467f-a8f5-2b3c4d5e6f70"


class FakeNovig:
    """Answers the two key routes and checks every signature against the mgmt key."""

    def __init__(self, mgmt: ed25519.Ed25519PrivateKey, fail_issue: bool = False) -> None:
        self.mgmt = mgmt
        self.fail_issue = fail_issue
        self.bodies: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = request.content
        canon = novig_auth.string_to_sign(
            int(request.headers["Novig-Timestamp"]),
            request.method,
            request.url.path,
            request.url.query.decode(),
            body,
        )
        self.mgmt.public_key().verify(
            base64.b64decode(request.headers["Novig-Signature"]), canon.encode()
        )
        assert request.headers["Novig-Key-Id"] == MGMT_ID
        assert request.headers["Content-Type"] == "application/json"
        payload = json.loads(body)
        self.bodies.append((request.url.path, payload))
        if request.url.path == "/v3/account/subaccounts":
            return httpx.Response(201, json={"keyId": SUB_ID, "label": "x", "balance": "0.00000"})
        if self.fail_issue:
            return httpx.Response(400, json={"code": "ACCOUNT_RULE_REFUSED", "message": "cap"})
        return httpx.Response(201, json={"keyId": READ_ID, "fingerprint": "sha256:00"})


@pytest.fixture
def mgmt(tmp_path: Path) -> tuple[ed25519.Ed25519PrivateKey, Path]:
    key = ed25519.Ed25519PrivateKey.generate()
    pem = tmp_path / "mgmt.pem"
    pem.write_bytes(novig_auth.private_key_pem(key))
    return key, pem


def _client(fake: FakeNovig) -> httpx.Client:
    return httpx.Client(base_url="https://api.paper.novig.com", transport=httpx.MockTransport(fake))


def test_open_then_issue_read_key(mgmt: tuple[ed25519.Ed25519PrivateKey, Path]) -> None:
    key, pem = mgmt
    fake = FakeNovig(key)
    signer = novig_auth.Signer.from_pem(MGMT_ID, pem)
    with _client(fake) as client:
        sub = novig_keys.open_subaccount(client, signer, "recorder")
        key_id, read_key = novig_keys.issue_read_key(client, signer, sub, "recorder")
    assert (sub, key_id) == (SUB_ID, READ_ID)
    (open_path, open_body), (issue_path, issue_body) = fake.bodies
    assert open_path == "/v3/account/subaccounts"
    assert "scope" not in open_body
    assert open_body["algorithm"] == "Ed25519"
    assert issue_path == f"/v3/account/subaccounts/{SUB_ID}/keys"
    assert issue_body["scope"] == "trading::read"
    assert issue_body["publicKey"] == novig_auth.public_key_pem(read_key)
    # The subaccount's trading key is a different, discarded key.
    assert open_body["publicKey"] != issue_body["publicKey"]


def test_api_error_carries_code(mgmt: tuple[ed25519.Ed25519PrivateKey, Path]) -> None:
    key, pem = mgmt
    signer = novig_auth.Signer.from_pem(MGMT_ID, pem)
    with (
        _client(FakeNovig(key, fail_issue=True)) as client,
        pytest.raises(novig_keys.NovigApiError) as err,
    ):
        novig_keys.issue_read_key(client, signer, SUB_ID, "r")
    assert err.value.status == 400
    assert err.value.code == "ACCOUNT_RULE_REFUSED"


def _load_script(monkeypatch: pytest.MonkeyPatch, fake: FakeNovig) -> ModuleType:
    spec = importlib.util.spec_from_file_location("create_read_key", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    real = httpx.Client

    def client(**kwargs: Any) -> httpx.Client:
        return real(transport=httpx.MockTransport(fake), **kwargs)

    monkeypatch.setattr(module.httpx, "Client", client)
    return module


def _argv(pem: Path, out: Path) -> list[str]:
    return ["--env", "paper", "--mgmt-key-id", MGMT_ID, "--mgmt-pem", str(pem), "--out", str(out)]


def test_script_prints_only_key_id(
    mgmt: tuple[ed25519.Ed25519PrivateKey, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    key, pem = mgmt
    fake = FakeNovig(key)
    out = tmp_path / "keys" / "read.pem"
    assert _load_script(monkeypatch, fake).main(_argv(pem, out)) == 0
    captured = capsys.readouterr()
    assert captured.out == f"{READ_ID}\n"
    assert captured.err == ""
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    saved = serialization.load_pem_private_key(out.read_bytes(), password=None)
    assert isinstance(saved, ed25519.Ed25519PrivateKey)
    assert novig_auth.public_key_pem(saved) == fake.bodies[1][1]["publicKey"]


def test_script_never_reads_env_for_mgmt_key(
    mgmt: tuple[ed25519.Ed25519PrivateKey, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key, _ = mgmt
    monkeypatch.setenv("NOVIG_PRIVATE_KEY_PATH", str(tmp_path / "nope.pem"))
    module = _load_script(monkeypatch, FakeNovig(key))
    with pytest.raises(SystemExit):
        module.main(["--env", "paper", "--out", str(tmp_path / "r.pem")])


def test_script_failure_removes_output(
    mgmt: tuple[ed25519.Ed25519PrivateKey, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    key, pem = mgmt
    out = tmp_path / "read.pem"
    module = _load_script(monkeypatch, FakeNovig(key, fail_issue=True))
    assert module.main(_argv(pem, out)) == 1
    assert not out.exists()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"--subaccount {SUB_ID}" in captured.err


def test_script_refuses_to_overwrite(
    mgmt: tuple[ed25519.Ed25519PrivateKey, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key, pem = mgmt
    out = tmp_path / "read.pem"
    out.write_text("keep me")
    fake = FakeNovig(key)
    assert _load_script(monkeypatch, fake).main(_argv(pem, out)) == 2
    assert out.read_text() == "keep me"
    assert fake.bodies == []
