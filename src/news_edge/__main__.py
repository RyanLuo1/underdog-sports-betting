"""Command-line entry point. Each service will get its own subcommand."""

import argparse

from news_edge import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="news-edge")
    parser.add_argument("--version", action="version", version=f"news-edge {__version__}")
    parser.parse_args(argv)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
