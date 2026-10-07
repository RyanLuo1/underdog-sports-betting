"""Classify stored posts with the rules parser and write the classifications table.

uv run python scripts/classify_posts.py
"""

import sys

from news_edge.classify import rules
from news_edge.classify.store import classify_posts
from news_edge.core.db import connect


def main() -> int:
    with connect() as conn:
        counts = classify_posts(conn, "UnderdogNFL", "NFL")
    total = sum(counts.values())
    print(f"{rules.MODEL} {rules.VERSION}: {total} posts")
    for status, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {status:18s} {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
