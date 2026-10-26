"""Files on the clipboard: offered with their names only, pulled by the shortcut into a folder,
streamed in pieces, recorded in the history."""

import os
import shutil
import tempfile
import threading
import time
import unittest

from core import file_transfer, protocol


class FileClipboard:
    def __init__(self, paths=()):
        self.paths = list(paths)

    def read_files(self):
        return list(self.paths)


def wait_for(condition, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


def wire(message):
    """A message as the other end reads it."""
    return protocol.parse_message(protocol.dumps_message(message))


class Pair:
    """A sending and a receiving FileShare joined back to back, as one link joins them."""

    def __init__(self, test, paths, destination, mangle=None, **receiver):
        root = test.root
        self.sent_kinds = []
        self.notes = []
        self.sender = file_transfer.FileShare(lambda: FileClipboard(paths), notify=self.notes.append,
                                              history_path=os.path.join(root, "sender-history.json"))
        self.receiver = file_transfer.FileShare(lambda: FileClipboard(), notify=self.notes.append,
                                                destination=lambda: destination,
                                                history_path=os.path.join(root, "receiver-history.json"), **receiver)

        def to_receiver(message):
            message = wire(message)
            if mangle is not None:
                message = mangle(message)
            self.sent_kinds.append(message.get("type"))
            self.receiver.receive("sender", message)
            return True

        def to_sender(message):
            self.sender.receive("receiver", wire(message))
            return True

        files = self.sender.offer("receiver", to_receiver, lambda: 0, name="Sender PC")
        self.files = files
        if files is not None:
            read = protocol.read_clipboard(wire(protocol.clipboard_msg(None, None, files)))
            self.receiver.arrived("sender", read["files"], to_sender, name="Sender PC")


class Pulling(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="beamer-files-")
        self.addCleanup(lambda: shutil.rmtree(file_transfer._long(self.root), ignore_errors=True))
        self.source = os.path.join(self.root, "source")
        self.target = os.path.join(self.root, "target")
        os.makedirs(os.path.join(self.source, "folder", "deep"))
        os.makedirs(os.path.join(self.source, "folder", "empty"))
        os.makedirs(self.target)
        self.big = os.urandom(protocol.FILE_CHUNK_BYTES * 3 + 123)
        with open(os.path.join(self.source, "big.bin"), "wb") as handle:
            handle.write(self.big)
        with open(os.path.join(self.source, "folder", "deep", "note.txt"), "wb") as handle:
            handle.write(b"hello")
        open(os.path.join(self.source, "folder", "zero.txt"), "wb").close()
        self.copied = [os.path.join(self.source, "big.bin"), os.path.join(self.source, "folder")]

    def done(self, share):
        return lambda: share.snapshot()["history"] and not share.snapshot()["active"]

    def test_copying_sends_names_only_and_the_shortcut_brings_the_files_into_the_open_folder(self):
        pair = Pair(self, self.copied, self.target)
        self.assertEqual(pair.sent_kinds, [])
        self.assertEqual(pair.receiver.snapshot()["available"]["count"], 3)
        self.assertTrue(pair.receiver.pull())
        self.assertTrue(wait_for(self.done(pair.receiver)), pair.notes)
        with open(os.path.join(self.target, "big.bin"), "rb") as handle:
            self.assertEqual(handle.read(), self.big)
        with open(os.path.join(self.target, "folder", "deep", "note.txt"), "rb") as handle:
            self.assertEqual(handle.read(), b"hello")
        self.assertEqual(os.path.getsize(os.path.join(self.target, "folder", "zero.txt")), 0)
        self.assertTrue(os.path.isdir(os.path.join(self.target, "folder", "empty")))
        self.assertEqual(sorted(os.listdir(self.target)), ["big.bin", "folder"])
        received = pair.receiver.snapshot()["history"][0]
        self.assertEqual((received["direction"], received["status"], received["peer"], received["count"]),
                         ("received", "done", "Sender PC", 3))
        self.assertEqual(received["folder"], self.target)
        self.assertTrue(wait_for(self.done(pair.sender)))
        self.assertEqual(pair.sender.snapshot()["history"][0]["direction"], "sent")

    def test_a_name_already_taken_gets_a_number_as_explorer_and_finder_give_it(self):
        with open(os.path.join(self.target, "big.bin"), "wb") as handle:
            handle.write(b"keep me")
        pair = Pair(self, [os.path.join(self.source, "big.bin")], self.target)
        pair.receiver.pull()
        self.assertTrue(wait_for(self.done(pair.receiver)), pair.notes)
        with open(os.path.join(self.target, "big.bin"), "rb") as handle:
            self.assertEqual(handle.read(), b"keep me")
        with open(os.path.join(self.target, "big (2).bin"), "rb") as handle:
            self.assertEqual(handle.read(), self.big)

    def test_no_open_folder_means_downloads(self):
        downloads = os.path.join(self.root, "downloads")
        pair = Pair(self, self.copied, None, folder=lambda: downloads)
        pair.receiver.pull()
        self.assertTrue(wait_for(self.done(pair.receiver)), pair.notes)
        self.assertEqual(sorted(os.listdir(downloads)), ["big.bin", "folder"])

    def test_nothing_copied_says_so(self):
        notes = []
        share = file_transfer.FileShare(lambda: FileClipboard(), notify=notes.append)
        self.assertFalse(share.pull())
        self.assertIn(file_transfer.SHORTCUT_TEXT, notes[0])

    def test_a_piece_out_of_place_fails_stops_the_sender_and_leaves_nothing(self):
        def mangle(message):
            if message.get("type") == protocol.MSG_FILE_DATA and message["data"]["o"] > 0:
                message["data"]["o"] += 1
            return message

        pair = Pair(self, self.copied, self.target, mangle=mangle)
        pair.receiver.pull()
        self.assertTrue(wait_for(self.done(pair.receiver)))
        self.assertEqual(pair.receiver.snapshot()["history"][0]["status"], "failed")
        self.assertTrue(wait_for(self.done(pair.sender)))
        self.assertNotIn(protocol.MSG_FILE_END, pair.sent_kinds)
        self.assertEqual(os.listdir(self.target), [])

    def test_pieces_nobody_asked_for_are_dropped(self):
        share = file_transfer.FileShare(lambda: FileClipboard(), destination=lambda: self.target)
        share.receive("stranger", wire(protocol.file_data_msg(5, 0, 0, b"x")))
        share.receive("stranger", wire(protocol.file_end_msg(5)))
        time.sleep(0.1)
        self.assertEqual(os.listdir(self.target), [])

    def test_a_request_for_files_never_offered_sends_nothing(self):
        pair = Pair(self, self.copied, self.target)
        pair.sender.receive("receiver", wire(protocol.file_request_msg(pair.files["x"] + 1)))
        time.sleep(0.1)
        self.assertEqual(pair.sent_kinds, [])

    def test_cancel_stops_both_ends(self):
        big = os.path.join(self.source, "huge.bin")
        with open(big, "wb") as handle:
            handle.write(os.urandom(protocol.FILE_CHUNK_BYTES * 200))
        gate = threading.Event()
        pair = Pair(self, [big], self.target)
        original = pair.receiver._write

        def slow(*args):
            gate.wait(2)
            original(*args)

        pair.receiver._write = slow
        pair.receiver.pull()
        self.assertTrue(wait_for(lambda: pair.receiver.snapshot()["active"]))
        pair.receiver.cancel()
        gate.set()
        self.assertTrue(wait_for(self.done(pair.receiver)))
        self.assertEqual(pair.receiver.snapshot()["history"][0]["status"], "cancelled")
        self.assertTrue(wait_for(self.done(pair.sender)))
        self.assertEqual(os.listdir(self.target), [])

    def test_a_folder_deeper_than_windows_paths_go_arrives(self):
        deep = os.path.join(self.source, "project", *(["a_rather_long_folder_name_" + str(n) for n in range(12)]))
        os.makedirs(file_transfer._long(deep))
        target = os.path.join(deep, "asset.uasset")
        self.assertGreater(len(target), 300)
        with open(file_transfer._long(target), "wb") as handle:
            handle.write(b"deep bytes")
        pair = Pair(self, [os.path.join(self.source, "project")], self.target)
        pair.receiver.pull()
        self.assertTrue(wait_for(self.done(pair.receiver)), pair.notes)
        self.assertEqual(pair.receiver.snapshot()["history"][0]["status"], "done", pair.notes)
        landed = os.path.join(self.target, os.path.relpath(target, self.source))
        with open(file_transfer._long(landed), "rb") as handle:
            self.assertEqual(handle.read(), b"deep bytes")

    def test_the_history_survives_a_restart(self):
        pair = Pair(self, self.copied, self.target)
        pair.receiver.pull()
        self.assertTrue(wait_for(self.done(pair.receiver)))
        path = os.path.join(self.root, "receiver-history.json")
        # The save follows the history in memory, on the thread that ended the transfer.
        self.assertTrue(wait_for(lambda: file_transfer.FileShare(lambda: FileClipboard(), history_path=path).snapshot()["history"]))
        again = file_transfer.FileShare(lambda: FileClipboard(), history_path=path)
        self.assertEqual(again.snapshot()["history"][0]["names"], ["big.bin", "folder"])
        again.clear_history()
        self.assertEqual(again.snapshot()["history"], [])

    def test_progress_shows_while_it_goes(self):
        big = os.path.join(self.source, "video.mp4")
        with open(big, "wb") as handle:
            handle.write(os.urandom(protocol.FILE_CHUNK_BYTES * 64))
        seen, shares = [], []
        pair = Pair(self, [big], self.target, changed=lambda: shares and seen.append(shares[0].snapshot()["active"]))
        shares.append(pair.receiver)
        file_transfer.CHANGED_EVERY_SECONDS, saved = 0.0, file_transfer.CHANGED_EVERY_SECONDS
        self.addCleanup(setattr, file_transfer, "CHANGED_EVERY_SECONDS", saved)
        pair.receiver.pull()
        self.assertTrue(wait_for(self.done(pair.receiver)))
        partial = [active[0]["done"] for active in seen if active and 0 < active[0]["done"] < active[0]["bytes"]]
        self.assertTrue(partial)


class Manifest(unittest.TestCase):
    def test_paths_that_climb_or_repeat_are_refused(self):
        for items in ([{"p": "../x", "s": 1}], [{"p": "/x", "s": 1}], [{"p": "a\\b", "s": 1}],
                      [{"p": "a", "s": 1}, {"p": "A", "s": 2}], [{"p": "a", "s": -1}], []):
            self.assertIsNone(protocol.read_files({"x": 1, "items": items}), items)
        self.assertIsNotNone(protocol.read_files({"x": 1, "items": [{"p": "a/b", "s": 0}, {"p": "c", "dir": True}]}))

    def test_names_windows_refuses_are_made_safe(self):
        self.assertEqual(file_transfer.safe_part('a:b?.txt'), "a_b_.txt")
        if os.name == "nt":
            self.assertEqual(file_transfer.safe_part("CON.txt"), "_CON.txt")
            self.assertEqual(file_transfer.safe_part("name. "), "name")

    def test_a_piece_fits_between_input_messages(self):
        message = protocol.file_data_msg(2**31 - 1, 9999, protocol.FILES_MAX_BYTES, b"\xff" * protocol.FILE_CHUNK_BYTES)
        self.assertLess(len(protocol.dumps_message(message)), protocol.SMALL_MESSAGE_BYTES)


class OverARealLink(unittest.TestCase):
    """Over a real, sealed link on loopback: the owner's files pulled by the shortcut on the machine
    it drives, and the machine's files pulled back by the owner."""

    def setUp(self):
        from core.tests.responder_harness import B, CAPS, Initiator, Machine, entry
        self.root = tempfile.mkdtemp(prefix="beamer-link-files-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.machine_folder = os.path.join(self.root, "machine-folder")
        self.owner_folder = os.path.join(self.root, "owner-folder")
        os.makedirs(self.machine_folder)
        os.makedirs(self.owner_folder)
        caps = sorted(set(CAPS) | {file_transfer.CAPABILITY})
        self.machine = Machine([entry(B, "Peer")], caps=caps)
        self.machine_files = []
        self.machine.clipboard.read_files = lambda: list(self.machine_files)
        self.machine.responder._files = file_transfer.FileShare(
            lambda: self.machine.clipboard, destination=lambda: self.machine_folder)
        self.machine.start()
        self.addCleanup(self.machine.stop)
        self.link = Initiator(self.machine)
        self.addCleanup(self.link.close)
        self.link.handshake(self.link.hello(caps=caps))
        self.owner_clipboard = FileClipboard()
        self.owner = file_transfer.FileShare(lambda: self.owner_clipboard, destination=lambda: self.owner_folder)

    def make(self, name, size):
        path = os.path.join(self.root, name)
        data = os.urandom(size)
        with open(path, "wb") as handle:
            handle.write(data)
        return path, data

    def pump(self, until, seconds=15):
        """Hands what the machine sends to the owner's FileShare until `until()`."""
        deadline = time.monotonic() + seconds
        while not until() and time.monotonic() < deadline:
            message = self.link._claim(lambda m: m.get("type") in protocol.FILE_TYPES, 0.1)
            if message is not None:
                self.owner.receive("machine", message)
        return until()

    def test_the_owner_s_files_come_into_the_folder_open_on_the_machine_it_drives(self):
        path, data = self.make("photo.jpg", 1_500_000)
        route = self.link.take()
        self.assertEqual(self.link.answer(route)[0], protocol.MSG_ACCEPT)
        self.owner_clipboard.paths = [path]
        post = lambda message: (self.link.send(message), True)[1]
        files = self.owner.offer("machine", post, lambda: 0)
        self.link.send(protocol.clipboard_msg(None, None, files))
        self.link.send(protocol.file_pull_msg())
        landed = os.path.join(self.machine_folder, "photo.jpg")
        self.assertTrue(self.pump(lambda: os.path.exists(landed) and not self.owner.snapshot()["active"]
                                  and self.owner.snapshot()["history"]))
        self.assertTrue(wait_for(lambda: os.path.exists(landed) and os.path.getsize(landed) == len(data)))
        with open(landed, "rb") as handle:
            self.assertEqual(handle.read(), data)

    def test_the_machine_s_files_come_to_the_owner_when_it_presses_the_shortcut_at_home(self):
        path, data = self.make("report.pdf", 300_000)
        route = self.link.take()
        self.assertEqual(self.link.answer(route)[0], protocol.MSG_ACCEPT)
        self.machine_files = [path]
        self.machine.clipboard.copy("names as text")
        self.link.let_go()
        clipboard = self.link.expect(protocol.MSG_CLIPBOARD, timeout=3)
        self.assertIsNotNone(clipboard)
        self.assertNotIn("text", clipboard)
        self.owner.arrived("machine", protocol.read_files(clipboard["files"]), lambda message: (self.link.send(message), True)[1])
        self.assertTrue(self.owner.pull())
        landed = os.path.join(self.owner_folder, "report.pdf")
        self.assertTrue(self.pump(lambda: bool(self.owner.snapshot()["history"])))
        with open(landed, "rb") as handle:
            self.assertEqual(handle.read(), data)


if __name__ == "__main__":
    unittest.main()
