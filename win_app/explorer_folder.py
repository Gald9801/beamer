"""The folder the user is in on this PC, for the files shortcut (core/file_transfer.py): the
Explorer window in front, or the desktop when that is what has focus; None for anything else,
which sends the files to Downloads/Beamer.

Explorer's windows are reached through the Shell.Application automation object, which names the
folder each one shows; the one whose window handle is the foreground window is the user's.

Some windows take the foreground without being where the user is: GameInput's SYSTEM-owned
window (see input_injector.py) and Beamer's own overlays. When one of those is in front, the
first visible top-level window under it in z-order stands for the user's.

On Windows 11 the tabs of one Explorer window share its handle, so each Shell.Application window
is matched by its own tab window against the tab being shown."""

import logging
import os
import sys

LOGGER = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"
_EXPLORER_CLASSES = ("CabinetWClass", "ExploreWClass")
_DESKTOP_CLASSES = ("Progman", "WorkerW")
_DESKTOP_DIRECTORY = 0x10  # ssfDESKTOPDIRECTORY
# Windows that hold the foreground without being one the user works in.
_NOT_USER_CLASSES = ("GameInputServiceWindow",)
_TAB_CLASS = "ShellTabWindowClass"
_GW_HWNDNEXT = 2
_DWMWA_CLOAKED = 14
# The walk is bounded: a desktop has hundreds of top-level windows, not thousands.
_MAX_WALK = 2000


def user_window(foreground, next_window, describe, own_pid=None):
    """The window that stands for where the user is, as (hwnd, class), or (0, "") for none.

    `foreground` itself unless it is not a user's window: GameInput's, one of this process's,
    hidden or cloaked; then the first window under it in z-order that is one. With no foreground,
    the walk starts at `next_window(0)`, the top of the z-order. `describe(hwnd)` gives
    (class, pid, shown) and `next_window(hwnd)` the window below it, 0 at the bottom."""
    own_pid = os.getpid() if own_pid is None else own_pid
    hwnd = foreground or next_window(0)
    for _ in range(_MAX_WALK):
        if not hwnd:
            break
        window_class, pid, shown = describe(hwnd)
        if shown and pid != own_pid and window_class not in _NOT_USER_CLASSES:
            return hwnd, window_class
        hwnd = next_window(hwnd)
    return 0, ""


def _describe(hwnd):
    import ctypes
    import win32gui
    import win32process

    window_class = win32gui.GetClassName(hwnd)
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    shown = bool(win32gui.IsWindowVisible(hwnd))
    if shown:
        cloaked = ctypes.c_int(0)
        if not ctypes.windll.dwmapi.DwmGetWindowAttribute(
                ctypes.c_void_p(hwnd), _DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)):
            shown = not cloaked.value
    return window_class, pid, shown


def _next_window(hwnd):
    import win32gui

    try:
        return win32gui.GetWindow(hwnd, _GW_HWNDNEXT) if hwnd else win32gui.GetTopWindow(0)
    except Exception:
        return 0


def _tab_handle(window):
    """The tab window a Shell.Application window draws in, or None."""
    import pythoncom
    from win32com.shell import shell

    try:
        provider = window._oleobj_.QueryInterface(pythoncom.IID_IServiceProvider)
        browser = provider.QueryService(shell.SID_STopLevelBrowser, shell.IID_IShellBrowser)
        return int(browser.GetWindow())
    except Exception:
        return None


def _explorer_path(shell, hwnd):
    import win32gui

    windows = []
    for window in shell.Windows():
        try:
            if int(window.HWND) == int(hwnd):
                windows.append(window)
        except Exception:
            continue
    if len(windows) > 1:
        # The tab being shown is the first tab window in the Explorer window's z-order.
        shown = win32gui.FindWindowEx(hwnd, 0, _TAB_CLASS, None)
        by_tab = [window for window in windows if shown and _tab_handle(window) == shown]
        if not by_tab:
            title = win32gui.GetWindowText(hwnd)
            by_tab = [window for window in windows if title.startswith(str(window.LocationName or "\0"))]
        windows = by_tab[:1]
    for window in windows:
        try:
            path = window.Document.Folder.Self.Path
        except Exception:
            return None
        # A virtual folder (This PC, Recycle Bin) has no path on disk to write into.
        return path if path and os.path.isdir(path) else None
    return None


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
    front_class = win32gui.GetClassName(foreground) if foreground else ""
    hwnd, window_class = user_window(foreground, _next_window, _describe)
    path = None
    if window_class in _EXPLORER_CLASSES + _DESKTOP_CLASSES:
        pythoncom.CoInitialize()
        try:
            shell = win32com.client.Dispatch("Shell.Application")
            if window_class in _DESKTOP_CLASSES:
                # The desktop folder itself, wherever OneDrive or a policy has moved it.
                path = shell.Namespace(_DESKTOP_DIRECTORY).Self.Path
                path = path if path and os.path.isdir(path) else None
            else:
                path = _explorer_path(shell, hwnd)
        except Exception:
            LOGGER.exception("Could not ask Explorer which folder is open")
        finally:
            pythoncom.CoUninitialize()
    LOGGER.info("Pull folder: foreground %s, user window %s, folder %s",
                front_class or "none", window_class or "none", path or "none (Downloads)")
    return path
