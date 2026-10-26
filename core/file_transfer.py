"""Files on the clipboard, carried to the machine the clipboard goes to (this fork's addition to
WIRE.md section 5).

Text and images ride inside the `clipboard` message itself. Files cannot: they may be gigabytes,
and the link also carries the input. So the `clipboard` names them (`files`: a transfer id and
each file's path and size), and their bytes follow on the same link as `file_data` pieces of
FILE_CHUNK_BYTES, sent only while the link's queue is nearly empty, so a mouse move never waits
behind more than a piece or two. `file_end` closes a transfer; `file_cancel` gives it up.

The receiver accepts pieces only for the transfer the last `clipboard` from that peer named, and
only a `clipboard` it took by the clipboard's own rules (an owner's, or one from a machine just let
go) names one, so a peer cannot write files here except by the clipboard path. Files land in a
hidden folder under Downloads/Beamer and are moved into a folder of their own when every byte has
arrived; only then are they put on the clipboard, and only when nothing else has been copied here
since the `clipboard` arrived. Paste then copies them where they are wanted.

Nothing here knows the platform: the clipboard modules read and set the file list
(`read_files()` and `set_files(paths)`), and the app hands in how to post on a link and how much
that link has queued.
"""

import collections
import itertools
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
# Pieces queued on a link before the sender waits: enough to keep a LAN busy, few enough that
# input queued behind them is not held up.
SEND_AHEAD = 3
SEND_POLL_SECONDS = 0.002
# Pieces waiting for the disk. A reader that gets ahead of this waits, which stops the link.
WRITE_QUEUE = 256
# A file name Windows refuses, and the characters it refuses in one; replaced so a Mac's name lands.
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(1, 10)), *(f"LPT{n}" for n in range(1, 10))}
_BAD_CHARS = set('<>:"|?*\\')


def downloads_dir():
    """Where received files go: Downloads/Beamer in the user's home."""
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
        if source is not None:
            sources.append(source)
            total += item["s"]
        else:
            sources.append(None)
        return True

    for path in paths:
        path = os.path.abspath(path)
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


class _Outgoing:
    def __init__(self, xfer, items, sources):
        self.xfer = xfer
        self.items = items
        self.sources = sources
        self.cancelled = threading.Event()


class _Incoming:
    def __init__(self, xfer, items, staging, stamp):
        self.xfer = xfer
        self.items = items
        self.staging = staging
        self.paths = []
        self.stamp = stamp
        self.index = -1          # the file being written
        self.offset = 0
        self.handle = None
        self.failed = False


class FileShare:
    """Both halves for one app: offers this machine's copied files, and takes a peer's.

    - `clipboard()`: the platform clipboard module, with `read_files()`, `set_files(paths)` and
      `change_stamp()`.
    - `written(stamp)`: told after files are put on the clipboard, with the clipboard's stamp, so
      the app counts that change as a peer's and does not send it back.
    - `notify(text)`: optional, a short line for the user (a transfer finished or failed)."""

    def __init__(self, clipboard, written=None, notify=None, folder=None):
        self._clipboard = clipboard
        self._written = written or (lambda stamp: None)
        self._notify = notify or (lambda text: None)
        self._folder = folder or downloads_dir
        self._lock = threading.Lock()
        self._ids = itertools.count(random.randint(1, 2**30))
        self._offers = {}        # peer: _Outgoing, offered and maybe sending
        self._incoming = {}      # peer: _Incoming
        self._writes = collections.deque()
        self._write_cond = threading.Condition(self._lock)
        self._writer = None

    # Sending

    def offer(self, peer):
        """The `files` for a clipboard going to `peer`, or None when no files are copied here. A
        newer offer to the same peer cancels the older one."""
        try:
            paths = self._clipboard().read_files()
        except Exception:
            LOGGER.exception("Could not read the copied files")
            return None
        if not paths:
            return None
        collected = collect(paths)
        if collected is None:
            LOGGER.warning("The copied files are past what Beamer carries; sending none")
            return None
        items, sources = collected
        with self._lock:
            xfer = next(self._ids) % protocol.FILE_ID_MAX + 1
            older = self._offers.get(peer)
            if older is not None:
                older.cancelled.set()
            self._offers[peer] = _Outgoing(xfer, items, sources)
        return {"x": xfer, "items": items}

    def send(self, peer, post, backlog):
        """Streams the files last offered to `peer`, after its `clipboard` was posted: `post(message)`
        on the same link, False once it is down; `backlog()` how many messages it has queued."""
        with self._lock:
            out = self._offers.get(peer)
        if out is None or out.cancelled.is_set():
            return
        threading.Thread(target=self._send, args=(peer, out, post, backlog),
                         name="Beamer-files-out", daemon=True).start()

    def _send(self, peer, out, post, backlog):
        started, sent = time.monotonic(), 0

        def put(message):
            while backlog() >= SEND_AHEAD:
                if out.cancelled.is_set():
                    return False
                time.sleep(SEND_POLL_SECONDS)
            return not out.cancelled.is_set() and post(message)

        try:
            for index, (item, source) in enumerate(zip(out.items, out.sources)):
                if source is None:
                    continue
                with open(source, "rb") as handle:
                    offset = 0
                    while offset < item["s"]:
                        chunk = handle.read(min(protocol.FILE_CHUNK_BYTES, item["s"] - offset))
                        if not chunk:
                            raise OSError(f"{source} became shorter while it was sent")
                        if not put(protocol.file_data_msg(out.xfer, index, offset, chunk)):
                            LOGGER.info("Stopped sending files: the link is down or a newer copy replaced them")
                            return
                        offset += len(chunk)
                        sent += len(chunk)
            if put(protocol.file_end_msg(out.xfer)):
                LOGGER.info("Sent %d files, %d bytes, in %.1fs", len(out.items), sent, time.monotonic() - started)
        except Exception:
            LOGGER.exception("Sending files failed")
            post(protocol.file_cancel_msg(out.xfer))
        finally:
            with self._lock:
                if self._offers.get(peer) is out:
                    del self._offers[peer]

    def link_lost(self, peer):
        """The link to `peer` went down: what was going there, and what was coming from it, stops."""
        with self._lock:
            out = self._offers.pop(peer, None)
            incoming = self._incoming.pop(peer, None)
        if out is not None:
            out.cancelled.set()
        if incoming is not None:
            self._queue(lambda: self._discard(incoming))

    # Receiving

    def arrived(self, peer, files):
        """A `clipboard` taken from `peer` named `files` (protocol.read_files): its pieces may come.
        A transfer still coming from that peer is given up."""
        try:
            stamp = self._clipboard().change_stamp()
        except Exception:
            stamp = None
        folder = self._folder()
        staging = os.path.join(folder, f".incoming-{files['x']}-{os.getpid()}")
        total = sum(item.get("s", 0) for item in files["items"])
        try:
            os.makedirs(staging, exist_ok=True)
            if shutil.disk_usage(staging).free < total + 64 * 1024**2:
                LOGGER.warning("Not enough disk space for %d bytes of files", total)
                shutil.rmtree(staging, ignore_errors=True)
                return
        except OSError:
            LOGGER.exception("Could not make a folder for the files")
            return
        incoming = _Incoming(files["x"], files["items"], staging, stamp)
        with self._lock:
            older = self._incoming.get(peer)
            self._incoming[peer] = incoming
        if older is not None:
            self._queue(lambda: self._discard(older))
        LOGGER.info("Receiving %d files, %d bytes", len(files["items"]), total)

    def receive(self, peer, message):
        """A `file_data`, `file_end` or `file_cancel` from `peer`; one for no transfer of its is dropped."""
        read = protocol.read_file_message(message)
        if read is None:
            return
        kind, xfer, index, offset, chunk = read
        with self._lock:
            incoming = self._incoming.get(peer)
            if incoming is None or incoming.xfer != xfer:
                return
            if kind != protocol.MSG_FILE_DATA:
                del self._incoming[peer]
        if kind == protocol.MSG_FILE_DATA:
            self._queue(lambda: self._write(incoming, index, offset, chunk), wait=True)
        elif kind == protocol.MSG_FILE_END:
            self._queue(lambda: self._finish(incoming))
        else:
            LOGGER.info("The sender gave the files up")
            self._queue(lambda: self._discard(incoming))

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
        parts = [safe_part(part) for part in incoming.items[index]["p"].split("/")]
        return os.path.join(incoming.staging, *parts)

    def _close_through(self, incoming, index):
        """Closes the file being written and makes every file before `index` that has no pieces
        (an empty file or a folder); False when one is short."""
        if incoming.handle is not None:
            incoming.handle.close()
            incoming.handle = None
            if incoming.offset != incoming.items[incoming.index]["s"]:
                return False
        for skipped in range(incoming.index + 1, index):
            item = incoming.items[skipped]
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
        try:
            if index != incoming.index:
                if index < incoming.index or index >= len(incoming.items) or incoming.items[index].get("dir"):
                    raise ValueError("a piece out of order")
                if not self._close_through(incoming, index):
                    raise ValueError("a file arrived short")
                target = self._target(incoming, index)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                incoming.handle = open(target, "wb")
                incoming.index, incoming.offset = index, 0
            if offset != incoming.offset or offset + len(chunk) > incoming.items[index]["s"]:
                raise ValueError("a piece out of place")
            incoming.handle.write(chunk)
            incoming.offset += len(chunk)
        except (OSError, ValueError) as exc:
            LOGGER.warning("Receiving files failed: %s", exc)
            self._fail(incoming)

    def _fail(self, incoming):
        incoming.failed = True
        self._discard(incoming)
        self._notify(tr("Files from the other machine could not be received"))

    def _discard(self, incoming):
        if incoming.handle is not None:
            try:
                incoming.handle.close()
            except OSError:
                pass
            incoming.handle = None
        shutil.rmtree(incoming.staging, ignore_errors=True)

    def _finish(self, incoming):
        if incoming.failed:
            return
        try:
            if not self._close_through(incoming, len(incoming.items)):
                raise ValueError("a file arrived short")
            final = self._final_folder()
            os.replace(incoming.staging, final)
        except (OSError, ValueError) as exc:
            LOGGER.warning("Receiving files failed: %s", exc)
            self._fail(incoming)
            return
        tops = []
        for item in incoming.items:
            top = safe_part(item["p"].split("/")[0])
            if top not in tops:
                tops.append(top)
        paths = [os.path.join(final, top) for top in tops]
        LOGGER.info("Received %d files into %s", len(incoming.items), final)
        clipboard = self._clipboard()
        try:
            if incoming.stamp is not None and clipboard.change_stamp() != incoming.stamp:
                LOGGER.info("Something else was copied meanwhile; the files stay in %s", final)
                self._notify(tr("Files received into {folder}").format(folder=final))
                return
            if clipboard.set_files(paths):
                self._written(clipboard.change_stamp())
                self._notify(tr("Files received: paste to copy them here"))
                return
        except Exception:
            LOGGER.exception("Could not put the received files on the clipboard")
        self._notify(tr("Files received into {folder}").format(folder=final))

    def _final_folder(self):
        base = os.path.join(self._folder(), time.strftime("%Y-%m-%d %H.%M.%S"))
        final, n = base, 2
        while os.path.exists(final):
            final, n = f"{base} ({n})", n + 1
        return final
