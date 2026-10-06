"""Open a Novig subaccount and issue a trading::read key on it. Run this yourself, once.

The management key is read only from --mgmt-pem, never from .env, and is used for these
two calls only. The new read key's private half is written to --out (mode 0600, never
overwritten). On success, stdout is just the new key ID.

    uv run python scripts/create_read_key.py --env paper \\
        --mgmt-key-id <management key ID> --mgmt-pem ~/secure/novig-mgmt.pem \\
        --out ~/.novig/paper-recorder.pem

Then set in .env: NOVIG_ENV, NOVIG_TRADING_KEY_ID (the printed ID), and
NOVIG_PRIVATE_KEY_PATH (the --out path).

The subaccount's own trading key is generated and discarded, so no order can ever be
placed from it. Pass --subaccount <its key ID> to add a read key to an existing one.
"""

import argparse
import os
import sys
from pathlib import Path

import httpx

from news_edge.market.novig_auth import HOSTS, Signer, private_key_pem
from news_edge.market.novig_keys import NovigApiError, issue_read_key, open_subaccount


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--env", choices=sorted(HOSTS), required=True)
    parser.add_argument("--mgmt-key-id", required=True, help="management key ID")
    parser.add_argument("--mgmt-pem", type=Path, required=True, help="management key .pem")
    parser.add_argument("--out", type=Path, required=True, help="where to write the read key")
    parser.add_argument("--label", default="recorder", help="subaccount label and key name")
    parser.add_argument("--subaccount", help="existing subaccount key ID to add the key to")
    args = parser.parse_args(argv)

    out: Path = args.out.expanduser()
    if out.exists():
        print(f"{out} already exists; choose a new --out path", file=sys.stderr)
        return 2
    signer = Signer.from_pem(args.mgmt_key_id, args.mgmt_pem.expanduser())

    # Claim the output file first, so a key is never issued with nowhere to save it.
    out.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    sub = args.subaccount
    try:
        with httpx.Client(base_url=HOSTS[args.env], timeout=30) as client:
            sub = sub or open_subaccount(client, signer, args.label)
            key_id, key = issue_read_key(client, signer, sub, args.label)
        os.write(fd, private_key_pem(key))
    except BaseException as exc:
        os.close(fd)
        out.unlink()
        if not isinstance(exc, NovigApiError | httpx.HTTPError):
            raise
        print(f"failed: {exc}", file=sys.stderr)
        if sub:
            print(f"retry with --subaccount {sub}", file=sys.stderr)
        return 1
    os.close(fd)
    print(key_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
