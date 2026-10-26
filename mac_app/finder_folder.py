"""The folder the user is in on this Mac, for the files shortcut (core/file_transfer.py): the front
Finder window's, or the Desktop when Finder is in front with no window; None when another app is
in front, which sends the files to Downloads/Beamer.

Finder is asked over Apple Events with osascript. The first time, macOS asks whether Beamer may
control Finder; refused, every pull goes to Downloads/Beamer."""

import logging
import os
import subprocess

LOGGER = logging.getLogger("Beamer")

_SCRIPT = """
tell application "System Events" to set frontApp to name of first application process whose frontmost is true
if frontApp is not "Finder" then return ""
tell application "Finder"
    if (count of Finder windows) is 0 then return POSIX path of (path to desktop folder)
    try
        return POSIX path of (target of front Finder window as alias)
    on error
        return ""
    end try
end tell
"""


def current():
    """The path of the folder in front, or None."""
    try:
        result = subprocess.run(["/usr/bin/osascript", "-e", _SCRIPT], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        LOGGER.exception("could not ask Finder which folder is open")
        return None
    path = result.stdout.strip()
    if result.returncode != 0:
        LOGGER.warning("Finder did not say which folder is open: %s", result.stderr.strip())
        return None
    return path if path and os.path.isdir(path) else None
