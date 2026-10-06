"""macOS notifications for unattended jobs."""

import logging
import subprocess

log = logging.getLogger(__name__)

# Title and message go in as arguments, never spliced into the script, so no text can
# break out of the AppleScript string.
_SCRIPT = [
    "-e",
    "on run argv",
    "-e",
    "display notification (item 2 of argv) with title (item 1 of argv)",
    "-e",
    "end run",
]


def notify(title: str, message: str) -> bool:
    """Show a macOS notification. Returns False (and logs) if it could not be sent."""
    try:
        subprocess.run(  # noqa: S603
            ["/usr/bin/osascript", *_SCRIPT, title, message],
            check=True,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("notification not sent: %s", exc)
        return False
    return True
