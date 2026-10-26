"""The folder the user is in on this PC, for the files shortcut (core/file_transfer.py): the
Explorer window in front, or the desktop when that is what has focus; None for anything else,
which sends the files to Downloads/Beamer.

Explorer's windows are reached through the Shell.Application automation object, which names the
folder each one shows; the one whose window handle is the foreground window is the user's."""

import logging
import os
import sys

LOGGER = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"
_EXPLORER_CLASSES = ("CabinetWClass", "ExploreWClass")
_DESKTOP_CLASSES = ("Progman", "WorkerW")
_DESKTOP_DIRECTORY = 0x10  # ssfDESKTOPDIRECTORY


def current():
    """The path of the folder in front, or None."""
    if not _IS_WINDOWS:
        return None
    try:
        import pythoncom
        import win32com.client
        import win32gui
    except ImportError:
        LOGGER.warning("pywin32 is missing; files go to Downloads")
        return None
    foreground = win32gui.GetForegroundWindow()
    if not foreground:
        return None
    window_class = win32gui.GetClassName(foreground)
    if window_class not in _EXPLORER_CLASSES + _DESKTOP_CLASSES:
        return None
    pythoncom.CoInitialize()
    try:
        shell = win32com.client.Dispatch("Shell.Application")
        if window_class in _DESKTOP_CLASSES:
            # The desktop folder itself, wherever OneDrive or a policy has moved it.
            path = shell.Namespace(_DESKTOP_DIRECTORY).Self.Path
            return path if path and os.path.isdir(path) else None
        for window in shell.Windows():
            try:
                if int(window.HWND) != int(foreground):
                    continue
                path = window.Document.Folder.Self.Path
            except Exception:
                continue
            # A virtual folder (This PC, Recycle Bin) has no path on disk to write into.
            if path and os.path.isdir(path):
                return path
            return None
    except Exception:
        LOGGER.exception("Could not ask Explorer which folder is open")
    finally:
        pythoncom.CoUninitialize()
    return None
