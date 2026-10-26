"""The Files page: files copied on another machine and ready to come here, the transfer under way
with its progress, and the history of what was sent and received (core/file_transfer.py)."""

import os
import time

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QProgressBar, QPushButton, QSizePolicy, QVBoxLayout, QWidget

import widgets
from core import file_transfer
from core.file_transfer import size_text
from core.i18n import tr

# History rows shown; the file keeps more.
HISTORY_SHOWN = 50


def _names(record) -> str:
    names = ", ".join(record.get("names") or [])
    more = record.get("more") or 0
    return tr("{names} and {count} more").format(names=names, count=more) if more else names


def _when(stamp) -> str:
    try:
        return time.strftime("%d %b %H:%M", time.localtime(float(stamp)))
    except (TypeError, ValueError, OverflowError):
        return ""


def history_line(record) -> str:
    """One history row's words: what went which way, with whom, how big, when, and how it ended."""
    size = size_text(record.get("bytes") or 0)
    count = record.get("count") or 0
    peer = record.get("peer") or tr("another machine")
    if record.get("direction") == "sent":
        line = tr("Sent {count} files ({size}) to {peer}").format(count=count, size=size, peer=peer)
    else:
        line = tr("Received {count} files ({size}) from {peer}").format(count=count, size=size, peer=peer)
    status = record.get("status")
    if status == "failed":
        line += " - " + tr("failed: {reason}").format(reason=record.get("reason") or "")
    elif status == "cancelled":
        line += " - " + tr("stopped")
    return f"{_when(record.get('at'))}  {line}"


class FilesPage:
    """Builds the page's modules into `layout` and refreshes them from FileShare.snapshot()."""

    def __init__(self, layout, files) -> None:
        self.files = files
        self._history_key = None
        self._rate = {}          # transfer: (time, bytes) last seen, for the speed

        how = widgets.Module(tr("How it works"))
        how.body.addWidget(widgets.label(tr(
            "Copy files or folders on one machine as you always do. Move to the other machine, open the "
            "folder you want them in, and press {shortcut}. They come straight into that folder. Pressed "
            "anywhere but a folder window, they go to Downloads\\Beamer.").format(shortcut=file_transfer.SHORTCUT_TEXT),
            "note", wrap=True))
        layout.addWidget(how)

        ready = widgets.Module(tr("Ready to bring here"))
        self.ready_text = widgets.label("", "body", wrap=True)
        ready.body.addWidget(self.ready_text)
        line = QHBoxLayout()
        self.ready_button = widgets.ActionButton(tr("Choose a folder..."))
        self.ready_button.clicked.connect(self._choose_folder)
        line.addWidget(self.ready_button)
        line.addStretch(1)
        ready.body.addLayout(line)
        layout.addWidget(ready)

        self.now = widgets.Module(tr("Under way"))
        self.now_text = widgets.label("", "body", wrap=True)
        self.now.body.addWidget(self.now_text)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(True)
        self.now.body.addWidget(self.bar)
        self.now_detail = widgets.label("", "note", wrap=True)
        self.now.body.addWidget(self.now_detail)
        line = QHBoxLayout()
        cancel = widgets.ActionButton(tr("Cancel"))
        cancel.clicked.connect(self.files.cancel)
        line.addWidget(cancel)
        line.addStretch(1)
        self.now.body.addLayout(line)
        layout.addWidget(self.now)

        history = widgets.Module(tr("History"))
        self.history_rows = QVBoxLayout()
        self.history_rows.setSpacing(6)
        history.body.addLayout(self.history_rows)
        self.history_empty = widgets.label(tr("Nothing sent or received yet."), "note", wrap=True)
        history.body.addWidget(self.history_empty)
        line = QHBoxLayout()
        self.clear_button = widgets.ActionButton(tr("Clear history"))
        self.clear_button.clicked.connect(self.files.clear_history)
        line.addWidget(self.clear_button)
        line.addStretch(1)
        history.body.addLayout(line)
        layout.addWidget(history)
        self.refresh()

    def _choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(None, tr("Bring the files into"))
        if folder:
            self.files.pull(os.path.normpath(folder))

    def refresh(self) -> None:
        snapshot = self.files.snapshot()
        available = snapshot["available"]
        if available is None:
            self.ready_text.setText(tr("Nothing yet. Copy files on another machine and come back here."))
            self.ready_button.setEnabled(False)
        else:
            self.ready_text.setText(tr("{count} files ({size}) copied on {peer}: {names}").format(
                count=available["count"], size=size_text(available["bytes"]),
                peer=available["peer"] or tr("another machine"), names=_names(available)))
            self.ready_button.setEnabled(True)

        active = snapshot["active"]
        self.now.setVisible(bool(active))
        if active:
            record = active[0]
            total, done = record["bytes"] or 0, record["done"] or 0
            peer = record["peer"] or tr("another machine")
            if record["direction"] == "sent":
                self.now_text.setText(tr("Sending {count} files to {peer}").format(count=record["count"], peer=peer))
            else:
                self.now_text.setText(tr("Receiving {count} files from {peer} into {folder}").format(
                    count=record["count"], peer=peer, folder=record["folder"] or ""))
            self.bar.setValue(int(1000 * done / total) if total else 0)
            self.bar.setFormat(f"{int(100 * done / total) if total else 0}%")
            # The speed over the last second or two, kept between refreshes so it does not flicker.
            key = (record["direction"], record["at"])
            now = time.monotonic()
            seen_at, seen, speed = self._rate.get(key, (now, done, None))
            if now - seen_at >= 1.0:
                speed = (done - seen) / (now - seen_at)
                seen_at, seen = now, done
            self._rate = {key: (seen_at, seen, speed)}
            detail = tr("{done} of {total}").format(done=size_text(done), total=size_text(total))
            if speed:
                detail += "  " + tr("{speed}/s").format(speed=size_text(int(speed)))
            self.now_detail.setText(detail)

        history = snapshot["history"][:HISTORY_SHOWN]
        key = (len(snapshot["history"]), history[0].get("ended") if history else None)
        if key != self._history_key:
            self._history_key = key
            self._fill_history(history)

    def _fill_history(self, history) -> None:
        while self.history_rows.count():
            item = self.history_rows.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.history_empty.setVisible(not history)
        self.clear_button.setEnabled(bool(history))
        for record in history:
            row = QWidget()
            line = QHBoxLayout(row)
            line.setContentsMargins(0, 0, 0, 0)
            text = widgets.label(history_line(record) + "\n" + _names(record), "note", wrap=True)
            text.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            line.addWidget(text, 1)
            folder = record.get("folder")
            if record.get("direction") == "received" and record.get("status") == "done" and folder:
                button = QPushButton(tr("Open folder"))
                button.setProperty("vernier", "small")
                button.clicked.connect(lambda _=False, path=folder: QDesktopServices.openUrl(QUrl.fromLocalFile(path)))
                line.addWidget(button, 0, Qt.AlignmentFlag.AlignTop)
            self.history_rows.addWidget(row)
