"""Files on the clipboard, pulled to another machine with a shortcut (this fork's addition to
WIRE.md section 5).

Copying files sends nothing but their names: the `clipboard` that crosses at a switch carries
`files` (a transfer id and each file's path and size), and the machine it reaches only remembers
them. The bytes move when the user, on the machine they want them on, opens a folder and presses
the files shortcut (Ctrl+Shift+Alt+V). That machine asks for them with `file_request`, and they
come on the same link as `file_data` pieces of FILE_CHUNK_BYTES, sent only while the link's queue
is nearly empty, so a mouse move never waits behind more than a piece or two; then `file_end`.
Either end may give the transfer up with `file_cancel`. The machine with the keyboard sees the
shortcut: pressed while its input is on another machine, it sends that machine `file_pull`, and
the shortcut is that machine's own.

The receiver writes into a hidden folder inside the destination and moves each copied item into
the destination when every byte has arrived, so a half-received folder never looks finished.
Pieces are taken only for a transfer this machine asked for, from the peer that offered it, and
only a `clipboard` taken by the clipboard's own rules names one.

Every transfer, sent or received, is kept in a short history for the app's window, which asks
`snapshot()` and is told of changes through `changed()`.

Nothing here knows the platform: the clipboard module reads the copied files (`read_files()`),
`destination()` names the folder the user is in, and the app hands in how to post on a link and
how much that link has queued.
"""

import collections
import itertools
import json
import logging
import os
import random
import shutil
import threading
import time
import unicodedata

from . import protocol
from .i18n import tr


LOGGER = logging.getLogger(__name__)

CAPABILITY = "clipboard_files"
# The shortcut, pressed on the machine the files should go to, in the folder they should go in.
SHORTCUT_TEXT = "Ctrl+Shift+Alt+V"
# Pieces queued on a link before the sender waits: enough to keep a LAN busy, few enough that
# input queued behind them is not held up.
SEND_AHEAD = 3
SEND_POLL_SECONDS = 0.002
# Pieces waiting for the disk. A reader that gets ahead of this waits, which stops the link.
WRITE_QUEUE = 256
# Transfers kept in the history, newest first.
HISTORY_LIMIT = 200
# The window hears of progress at most this often.
CHANGED_EVERY_SECONDS = 0.25
# A file name Windows refuses, and the characters it refuses in one; replaced so a Mac's name lands.
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(1, 10)), *(f"LPT{n}" for n in range(1, 10))}
_BAD_CHARS = set('<>:"|?*\\')
_EXTENDED = "\\\\?\\"
_EXTENDED_UNC = "\\\\?\\UNC\\"


def _long(path):
    """`path` as Windows takes one past MAX_PATH (260 characters): a deep project folder passes it
    easily. Elsewhere, and already extended, unchanged."""
    if os.name != "nt" or path.startswith(_EXTENDED):
        return path
    path = os.path.abspath(path)
    if path.startswith("\\\\"):
        return _EXTENDED_UNC + path[2:]
    return _EXTENDED + path


def _short(path):
    """The plain form of a path _long made, for the user."""
    if path.startswith(_EXTENDED_UNC):
        return "\\\\" + path[len(_EXTENDED_UNC):]
    return path[len(_EXTENDED):] if path.startswith(_EXTENDED) else path


def _hide(path):
    """Hides the folder files arrive in until they are whole: a dot hides it on a Mac, an
    attribute on Windows."""
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetFileAttributesW(path, 0x2)
        except (AttributeError, OSError):
            pass


def size_text(size):
    for unit, step in (("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
        if size >= step:
            return f"{size / step:.1f} {unit}"
    return f"{size} B"


def downloads_dir():
    """Where received files go when no folder is open: Downloads/Beamer in the user's home."""
    return os.path.join(os.path.expanduser("~"), "Downloads", "Beamer")


def collect(paths):
    """The `items` for the files and folders `paths` names, with the absolute source of each file,
    as (items, sources); None when there is nothing to send or it is past the limits. Folders are
    walked; links are not followed; files that cannot be read are left out."""
    items, sources, total, seen = [], [], 0, set()

    def add(item, source=None):
        nonlocal total
        key = item["p"].casefold()
        if key in seen or len(items) >= protocol.FILES_MAX_ITEMS:
            return False
        seen.add(key)
        items.append(item)
        sources.append(source)
        if source is not None:
            total += item["s"]
        return True

    for path in paths:
        path = _long(os.path.abspath(path))
        name = os.path.basename(path.rstrip("\\/"))
        if not name:
            continue
        if os.path.isfile(path) and not os.path.islink(path):
            try:
                if not add({"p": name, "s": os.path.getsize(path)}, path):
                    return None
            except OSError:
                continue
        elif os.path.isdir(path) and not os.path.islink(path):
            for root, dirs, files in os.walk(path):
                dirs[:] = sorted(d for d in dirs if not os.path.islink(os.path.join(root, d)))
                rel_root = os.path.relpath(root, os.path.dirname(path)).replace(os.sep, "/")
                if not dirs and not files:
                    if not add({"p": rel_root, "dir": True}):
                        return None
                for file in sorted(files):
                    full = os.path.join(root, file)
                    if os.path.islink(full):
                        continue
                    try:
                        if not add({"p": f"{rel_root}/{file}", "s": os.path.getsize(full)}, full):
                            return None
                    except OSError:
                        continue
    if not items or total > protocol.FILES_MAX_BYTES:
        return None
    if any(len(item["p"]) > protocol.FILE_PATH_MAX for item in items):
        return None
    return items, sources


def safe_part(part):
    """One path component as a name this platform can hold."""
    part = unicodedata.normalize("NFC", part)
    part = "".join("_" if c in _BAD_CHARS or ord(c) < 32 else c for c in part)
    if os.name == "nt":
        part = part.rstrip(" .")
        if part.split(".")[0].upper() in _RESERVED:
            part = "_" + part
    return part[:255] or "_"


def _tops(items):
    """The copied items themselves, the first part of each path, in order."""
    tops = []
    for item in items:
        top = safe_part(item["p"].split("/")[0])
        if top not in tops:
            tops.append(top)
    return tops


def _free_name(folder, name):
    """`name` in `folder`, or "name (2)" and on when that is taken, as Explorer and Finder do."""
    stem, ext = os.path.splitext(name)
    candidate, n = name, 2
    while os.path.exists(os.path.join(folder, candidate)):
        candidate, n = f"{stem} ({n}){ext}", n + 1
    return candidate


class Transfer:
    """One transfer, as the window shows it and the history keeps it."""

    def __init__(self, xfer, direction, peer_name, items):
        self.xfer = xfer
        self.direction = direction          # "sent" or "received"
        self.peer_name = peer_name or ""
        self.items = items
        self.count = sum(1 for item in items if not item.get("dir"))
        self.total = sum(item.get("s", 0) for item in items)
        self.names = _tops(items)
        self.done = 0
        self.folder = None
        self.status = "active"              # then "done", "failed" or "cancelled"
        self.reason = ""
        self.started = time.time()
        self.ended = None

    def record(self):
        return {"at": self.started, "ended": self.ended, "direction": self.direction, "peer": self.peer_name,
                "count": self.count, "bytes": self.total, "names": self.names[:5], "more": max(0, len(self.names) - 5),
                "folder": self.folder, "status": self.status, "reason": self.reason}


class _Outgoing:
    def __init__(self, transfer, sources, post, backlog):
        self.transfer = transfer
        self.sources = sources
        self.post = post
        self.backlog = backlog
        self.cancelled = threading.Event()
        self.started = False


class _Incoming:
    def __init__(self, transfer, staging, reply):
        self.transfer = transfer
        self.staging = staging
        self.reply = reply
        self.index = -1          # the file being written
        self.offset = 0
        self.handle = None
        self.failed = False


class FileShare:
    """Both halves for one app: offers this machine's copied files, and pulls a peer's.

    - `clipboard()`: the platform clipboard module, with `read_files()`.
    - `destination()`: optional, the folder the user is in on this machine (the front Explorer or
      Finder window), or None; files then go to Downloads/Beamer.
    - `notify(text)`: optional, a short line for the user (a transfer began, finished or failed).
    - `changed()`: optional, called when snapshot() would say something new, from any thread.
    - `history_path`: optional, a JSON file the history is kept in across restarts."""

    def __init__(self, clipboard, destination=None, notify=None, changed=None, history_path=None, folder=None):
        self._clipboard = clipboard
        self._destination = destination or (lambda: None)
        self._notify = notify or (lambda text: None)
        self._changed = changed or (lambda: None)
        self._folder = folder or downloads_dir
        self._history_path = history_path
        self._history_lock = threading.Lock()
        self._lock = threading.Lock()
        self._ids = itertools.count(random.randint(1, 2**30))
        self._offers = {}        # peer: _Outgoing, offered and maybe sending
        self._incoming = {}      # peer: _Incoming, asked for and arriving
        self._available = None   # (peer, files, reply, name): the last files a peer offered here
        self._history = self._load_history()
        self._writes = collections.deque()
        self._write_cond = threading.Condition(self._lock)
        self._writer = None
        self._changed_at = 0.0

    # What the window shows

    def snapshot(self):
        """{active: [record with `done`], available: {peer, count, bytes, names, more} or None,
        history: [record]}, newest first."""
        with self._lock:
            active = [out.transfer for out in self._offers.values() if out.started]
            active += [incoming.transfer for incoming in self._incoming.values()]
            available = None
            if self._available is not None:
                _, files, _, name = self._available
                probe = Transfer(0, "received", name, files["items"])
                available = {"peer": probe.peer_name, "count": probe.count, "bytes": probe.total,
                             "names": probe.names[:5], "more": max(0, len(probe.names) - 5)}
            records = [dict(t.record(), done=t.done) for t in active]
            return {"active": records, "available": available, "history": list(self._history)}

    def _tell(self, force=False):
        now = time.monotonic()
        if not force and now - self._changed_at < CHANGED_EVERY_SECONDS:
            return
        self._changed_at = now
        try:
            self._changed()
        except Exception:
            LOGGER.exception("The window could not be told of a file transfer")

    def _load_history(self):
        if not self._history_path:
            return []
        try:
            with open(self._history_path, encoding="utf-8") as handle:
                history = json.load(handle)
            return [entry for entry in history if isinstance(entry, dict)][:HISTORY_LIMIT]
        except (OSError, ValueError, TypeError):
            return []

    def _end(self, transfer, status, reason=""):
        transfer.status, transfer.reason, transfer.ended = status, str(reason), time.time()
        with self._lock:
            self._history.insert(0, transfer.record())
            del self._history[HISTORY_LIMIT:]
        self._save_history()
        self._tell(force=True)

    def _save_history(self):
        if not self._history_path:
            return
        # One save at a time, of the latest history: Windows refuses a replace while another holds
        # the file, so two transfers ending together must not race.
        with self._history_lock:
            with self._lock:
                history = list(self._history)
            temporary = self._history_path + ".tmp"
            for attempt in range(5):
                try:
                    os.makedirs(os.path.dirname(self._history_path), exist_ok=True)
                    with open(temporary, "w", encoding="utf-8") as handle:
                        json.dump(history, handle, ensure_ascii=False, indent=1)
                    os.replace(temporary, self._history_path)
                    return
                except OSError:
                    if attempt == 4:
                        LOGGER.exception("Could not save the file transfer history")
                    time.sleep(0.05)

    def clear_history(self):
        with self._lock:
            self._history = []
        if self._history_path:
            try:
                os.remove(self._history_path)
            except OSError:
                pass
        self._tell(force=True)

    # Sending

    def offer(self, peer, post, backlog, name=None):
        """The `files` for a clipboard going to `peer`, or None when no files are copied here. They
        go when `peer` asks (file_request): `post(message)` on the same link, False once it is down,
        and `backlog()`, how many messages it has queued. A newer offer to the same peer replaces
        the older one, and stops it if it was sending."""
        try:
            paths = self._clipboard().read_files()
        except Exception:
            LOGGER.exception("Could not read the copied files")
            return None
        if not paths:
            return None
        collected = collect(paths)
        if collected is None:
            LOGGER.warning("The copied files are past what Beamer carries; offering none")
            return None
        items, sources = collected
        with self._lock:
            xfer = next(self._ids) % protocol.FILE_ID_MAX + 1
            older = self._offers.get(peer)
            self._offers[peer] = _Outgoing(Transfer(xfer, "sent", name, items), sources, post, backlog)
        if older is not None and older.started:
            older.cancelled.set()
        LOGGER.info("Offered %d copied files to %s", len(items), name or "a peer")
        return {"x": xfer, "items": items}

    def _start_sending(self, peer, xfer):
        with self._lock:
            out = self._offers.get(peer)
            if out is None or out.transfer.xfer != xfer or out.started:
                return
            out.started = True
        threading.Thread(target=self._send, args=(peer, out), name="Beamer-files-out", daemon=True).start()
        self._tell(force=True)

    def _send(self, peer, out):
        transfer = out.transfer
        started = time.monotonic()
        LOGGER.info("Sending %d files, %s", transfer.count, size_text(transfer.total))

        def put(message):
            while out.backlog() >= SEND_AHEAD:
                if out.cancelled.is_set():
                    return False
                time.sleep(SEND_POLL_SECONDS)
            return not out.cancelled.is_set() and out.post(message)

        try:
            for index, (item, source) in enumerate(zip(transfer.items, out.sources)):
                if source is None:
                    continue
                with open(source, "rb") as handle:
                    offset = 0
                    while offset < item["s"]:
                        chunk = handle.read(min(protocol.FILE_CHUNK_BYTES, item["s"] - offset))
                        if not chunk:
                            raise OSError(f"{_short(source)} became shorter while it was sent")
                        if not put(protocol.file_data_msg(transfer.xfer, index, offset, chunk)):
                            LOGGER.info("Stopped sending files: given up, or the link is down")
                            self._end(transfer, "cancelled")
                            return
                        offset += len(chunk)
                        transfer.done += len(chunk)
                        self._tell()
            if put(protocol.file_end_msg(transfer.xfer)):
                LOGGER.info("Sent %d files, %d bytes, in %.1fs", transfer.count, transfer.total, time.monotonic() - started)
                self._end(transfer, "done")
            else:
                self._end(transfer, "cancelled")
        except Exception as exc:
            LOGGER.exception("Sending files failed")
            out.post(protocol.file_cancel_msg(transfer.xfer))
            self._notify(tr("Files could not be sent: {reason}").format(reason=exc))
            self._end(transfer, "failed", exc)
        finally:
            with self._lock:
                if self._offers.get(peer) is out:
                    del self._offers[peer]

    def link_lost(self, peer):
        """The link to `peer` went down: what was going there, and what was coming from it, stops,
        and what it offered can no longer be asked for."""
        with self._lock:
            out = self._offers.pop(peer, None)
            incoming = self._incoming.pop(peer, None)
            if self._available is not None and self._available[0] == peer:
                self._available = None
        if out is not None:
            out.cancelled.set()
        if incoming is not None:
            self._queue(lambda: self._give_up(incoming, "cancelled", tr("the link to the other machine went down")))
        self._tell(force=True)

    # Receiving

    def arrived(self, peer, files, reply, name=None):
        """A `clipboard` taken from `peer` named `files` (protocol.read_files): they may be pulled
        here. `reply(message)` posts on the link they would come on."""
        with self._lock:
            self._available = (peer, files, reply, name)
        LOGGER.info("%d copied files on %s can be pulled here", len(files["items"]), name or "a peer")
        self._tell(force=True)

    def forget(self, peer):
        """Something other than files was copied on `peer`: its files are no longer on offer."""
        with self._lock:
            if self._available is not None and self._available[0] == peer:
                self._available = None
        self._tell(force=True)

    def pull(self, folder=None):
        """The shortcut on this machine: asks for the files last copied on a peer, into `folder`, or
        the folder the user is in, or Downloads/Beamer. False when nothing is on offer."""
        with self._lock:
            available = self._available
            busy = available is not None and available[0] in self._incoming
        if available is None:
            self._notify(tr("No files are copied on another machine. Copy them there first, then press {shortcut} here.")
                         .format(shortcut=SHORTCUT_TEXT))
            return False
        if busy:
            self._notify(tr("Files are already on their way here"))
            return False
        peer, files, reply, name = available
        if folder is None:
            try:
                folder = self._destination()
            except Exception:
                LOGGER.exception("Could not tell which folder is open")
                folder = None
        folder = _long(folder or self._folder())
        transfer = Transfer(files["x"], "received", name, files["items"])
        transfer.folder = _short(folder)
        staging = os.path.join(folder, f".beamer-incoming-{files['x']}")
        down = tr("the link to the other machine went down")
        try:
            os.makedirs(staging, exist_ok=True)
            _hide(staging)
            free = shutil.disk_usage(staging).free
        except OSError as exc:
            LOGGER.exception("Could not make a folder for the files")
            self._notify(tr("Files could not be received: {reason}").format(reason=exc))
            self._end(transfer, "failed", exc)
            return False
        if free < transfer.total + 64 * 1024**2:
            shutil.rmtree(staging, ignore_errors=True)
            reason = tr("Not enough disk space for {size} of files").format(size=size_text(transfer.total))
            self._notify(reason)
            self._end(transfer, "failed", reason)
            return False
        incoming = _Incoming(transfer, staging, reply)
        with self._lock:
            self._incoming[peer] = incoming
        if not reply(protocol.file_request_msg(files["x"])):
            with self._lock:
                self._incoming.pop(peer, None)
            shutil.rmtree(staging, ignore_errors=True)
            self._notify(tr("Files could not be received: {reason}").format(reason=down))
            self._end(transfer, "failed", down)
            return False
        LOGGER.info("Pulling %d files, %d bytes, into %s", transfer.count, transfer.total, transfer.folder)
        self._notify(tr("Receiving {count} files ({size}) into {folder}").format(
            count=transfer.count, size=size_text(transfer.total), folder=transfer.folder))
        self._tell(force=True)
        return True

    def cancel(self):
        """The window's Cancel: every transfer under way here, either way, stops."""
        with self._lock:
            outgoing = [out for out in self._offers.values() if out.started]
            incoming = list(self._incoming.items())
            for peer, _ in incoming:
                del self._incoming[peer]
        for out in outgoing:
            out.post(protocol.file_cancel_msg(out.transfer.xfer))
            out.cancelled.set()
        for _, current in incoming:
            current.reply(protocol.file_cancel_msg(current.transfer.xfer))
            self._queue(lambda current=current: self._give_up(current, "cancelled", tr("cancelled here")))

    def receive(self, peer, message):
        """A `file_request`, `file_data`, `file_end` or `file_cancel` from `peer`; one for no
        transfer of its is dropped."""
        read = protocol.read_file_message(message)
        if read is None:
            return
        kind, xfer, index, offset, chunk = read
        if kind == protocol.MSG_FILE_REQUEST:
            self._start_sending(peer, xfer)
            return
        with self._lock:
            incoming = self._incoming.get(peer)
            if incoming is None or incoming.transfer.xfer != xfer:
                out = self._offers.get(peer)
                if kind == protocol.MSG_FILE_CANCEL and out is not None and out.transfer.xfer == xfer:
                    LOGGER.info("The other machine gave the files up; stopped sending")
                    out.cancelled.set()
                return
            if kind != protocol.MSG_FILE_DATA:
                del self._incoming[peer]
        if kind == protocol.MSG_FILE_DATA:
            self._queue(lambda: self._write(incoming, index, offset, chunk), wait=True)
        elif kind == protocol.MSG_FILE_END:
            self._queue(lambda: self._finish(incoming))
        else:
            LOGGER.info("The sender gave the files up")
            self._queue(lambda: self._give_up(incoming, "cancelled", tr("the other machine stopped sending")))

    def _queue(self, job, wait=False):
        with self._write_cond:
            while wait and len(self._writes) >= WRITE_QUEUE:
                self._write_cond.wait(0.1)
            self._writes.append(job)
            self._write_cond.notify_all()
            if self._writer is None:
                self._writer = threading.Thread(target=self._work, name="Beamer-files-in", daemon=True)
                self._writer.start()

    def _work(self):
        while True:
            with self._write_cond:
                while not self._writes:
                    if not self._write_cond.wait(30):
                        self._writer = None
                        return
                job = self._writes.popleft()
                self._write_cond.notify_all()
            try:
                job()
            except Exception:
                LOGGER.exception("Writing received files failed")

    def _target(self, incoming, index):
        parts = [safe_part(part) for part in incoming.transfer.items[index]["p"].split("/")]
        return os.path.join(incoming.staging, *parts)

    def _close_through(self, incoming, index):
        """Closes the file being written and makes every file before `index` that has no pieces
        (an empty file or a folder); False when one is short."""
        items = incoming.transfer.items
        if incoming.handle is not None:
            incoming.handle.close()
            incoming.handle = None
            if incoming.offset != items[incoming.index]["s"]:
                return False
        for skipped in range(incoming.index + 1, index):
            item = items[skipped]
            target = self._target(incoming, skipped)
            if item.get("dir"):
                os.makedirs(target, exist_ok=True)
            elif item["s"] == 0:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                open(target, "wb").close()
            else:
                return False
        return True

    def _write(self, incoming, index, offset, chunk):
        if incoming.failed:
            return
        items = incoming.transfer.items
        try:
            if index != incoming.index:
                if index < incoming.index or index >= len(items) or items[index].get("dir"):
                    raise ValueError("a piece out of order")
                if not self._close_through(incoming, index):
                    raise ValueError("a file arrived short")
                target = self._target(incoming, index)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                incoming.handle = open(target, "wb")
                incoming.index, incoming.offset = index, 0
            if offset != incoming.offset or offset + len(chunk) > items[index]["s"]:
                raise ValueError("a piece out of place")
            incoming.handle.write(chunk)
            incoming.offset += len(chunk)
            incoming.transfer.done += len(chunk)
            self._tell()
        except (OSError, ValueError) as exc:
            LOGGER.warning("Receiving files failed: %s", exc)
            self._fail(incoming, exc)

    def _fail(self, incoming, reason):
        incoming.failed = True
        with self._lock:
            for peer, current in list(self._incoming.items()):
                if current is incoming:
                    del self._incoming[peer]
        try:
            incoming.reply(protocol.file_cancel_msg(incoming.transfer.xfer))
        except Exception:
            LOGGER.exception("Could not tell the sender to stop")
        self._notify(tr("Files could not be received: {reason}").format(reason=reason))
        self._give_up(incoming, "failed", reason)

    def _give_up(self, incoming, status, reason):
        if incoming.handle is not None:
            try:
                incoming.handle.close()
            except OSError:
                pass
            incoming.handle = None
        shutil.rmtree(incoming.staging, ignore_errors=True)
        if incoming.transfer.status == "active":
            self._end(incoming.transfer, status, reason)

    def _finish(self, incoming):
        if incoming.failed:
            return
        transfer = incoming.transfer
        try:
            if not self._close_through(incoming, len(transfer.items)):
                raise ValueError("a file arrived short")
            folder = os.path.dirname(incoming.staging)
            landed = []
            for top in transfer.names:
                source = os.path.join(incoming.staging, top)
                if not os.path.exists(source):
                    continue
                name = _free_name(folder, top)
                os.replace(source, os.path.join(folder, name))
                landed.append(name)
            shutil.rmtree(incoming.staging, ignore_errors=True)
        except (OSError, ValueError) as exc:
            LOGGER.warning("Receiving files failed: %s", exc)
            self._fail(incoming, exc)
            return
        transfer.names = landed or transfer.names
        LOGGER.info("Received %d files into %s", transfer.count, transfer.folder)
        self._notify(tr("Files received into {folder}").format(folder=transfer.folder))
        self._end(transfer, "done")
