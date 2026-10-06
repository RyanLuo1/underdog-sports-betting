"""Open a Novig subaccount and issue a trading::read key on it.

Signed with the management key, which only scripts/create_read_key.py loads, from a path
given on its command line. Nothing in the running system holds the management key.

Routes: https://docs.novig.com/api/subaccounts/manage.md
"""

import json
from typing import Any

import httpx
from cryptography.hazmat.primitives.asymmetric import ed25519

from news_edge.market.novig_auth import Signer, public_key_pem


class NovigApiError(RuntimeError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"HTTP {status} {code}: {message}")
        self.status = status
        self.code = code


def signed_post(
    client: httpx.Client, signer: Signer, path: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """POST a JSON body, signing the exact bytes sent."""
    body = json.dumps(payload, separators=(",", ":")).encode()
    headers = signer.headers("POST", path, body=body)
    headers["Content-Type"] = "application/json"
    resp = client.post(path, content=body, headers=headers)
    if resp.status_code >= 400:
        try:
            err = resp.json()
            code, message = str(err.get("code", "")), str(err.get("message", ""))
        except ValueError:
            code, message = "", resp.text[:200]
        raise NovigApiError(resp.status_code, code, message)
    result: dict[str, Any] = resp.json()
    return result


def open_subaccount(client: httpx.Client, signer: Signer, label: str) -> str:
    """Open a subaccount and return its address (its trading key ID).

    The trading key's private half is generated here and never saved, so nothing can
    place orders from this subaccount. It exists only to carry read keys."""
    trading = ed25519.Ed25519PrivateKey.generate()
    body = {"label": label, "publicKey": public_key_pem(trading), "algorithm": "Ed25519"}
    return str(signed_post(client, signer, "/v3/account/subaccounts", body)["keyId"])


def issue_read_key(
    client: httpx.Client, signer: Signer, subaccount: str, name: str
) -> tuple[str, ed25519.Ed25519PrivateKey]:
    key = ed25519.Ed25519PrivateKey.generate()
    body = {
        "name": name,
        "publicKey": public_key_pem(key),
        "algorithm": "Ed25519",
        "scope": "trading::read",
    }
    created = signed_post(client, signer, f"/v3/account/subaccounts/{subaccount}/keys", body)
    return str(created["keyId"]), key
