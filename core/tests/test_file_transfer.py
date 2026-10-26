"""Files on the clipboard: offered on one machine, streamed in pieces, assembled on the other."""

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
        self.stamp = 1

    def read_files(self):
        return list(self.paths)

    def set_files(self, paths):
        self.paths = list(paths)
        self.stamp += 1
        return True

    def change_stamp(self):
        return self.stamp

    def copy(self):
        self.stamp += 1


def wait_for(condition, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


class FileTransfer(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="beamer-files-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.source = os.path.join(self.root, "source")
        self.inbox = os.path.join(self.root, "inbox")
        os.makedirs(os.path.join(self.source, "folder", "deep"))
        os.makedirs(os.path.join(self.source, "folder", "empty"))
        self.big = os.urandom(protocol.FILE_CHUNK_BYTES * 3 + 123)
        with open(os.path.join(self.source, "big.bin"), "wb") as handle:
            handle.write(self.big)
        with open(os.path.join(self.source, "folder", "deep", "note.txt"), "wb") as handle:
            handle.write(b"hello")
        open(os.path.join(self.source, "folder", "zero.txt"), "wb").close()
        self.sending_clipboard = FileClipboard([os.path.join(self.source, "big.bin"), os.path.join(self.source, "folder")])
        self.receiving_clipboard = FileClipboard()
        self.written = []
        self.notes = []
        self.sender = file_transfer.FileShare(lambda: self.sending_clipboard)
        self.receiver = file_transfer.FileShare(lambda: self.receiving_clipboard, written=self.written.append,
                                                notify=self.notes.append, folder=lambda: self.inbox)

    def carry(self, mangle=None):
        """Offers, then posts each message straight to the receiver as a link would deliver it."""
        files = self.sender.offer("peer")
        self.assertIsNotNone(files)
        message = protocol.parse_message(protocol.dumps_message(protocol.clipboard_msg(None, None, files)))
        read = protocol.read_clipboard(message)
        self.receiver.arrived("peer", read["files"])
        posted = []

        def post(message):
            message = protocol.parse_message(protocol.dumps_message(message))
            if mangle is not None:
                message = mangle(message)
            posted.append(message.get("type"))
            self.receiver.receive("peer", message)
            return True

        self.sender.send("peer", post, lambda: 0)
        return posted

    def test_files_and_folders_arrive_whole_and_go_on_the_clipboard(self):
        self.carry()
        self.assertTrue(wait_for(lambda: self.written), self.notes)
        paths = self.receiving_clipboard.paths
        self.assertEqual(sorted(os.path.basename(path) for path in paths), ["big.bin", "folder"])
        folder = os.path.dirname(paths[0])
        with open(os.path.join(folder, "big.bin"), "rb") as handle:
            self.assertEqual(handle.read(), self.big)
        with open(os.path.join(folder, "folder", "deep", "note.txt"), "rb") as handle:
            self.assertEqual(handle.read(), b"hello")
        self.assertEqual(os.path.getsize(os.path.join(folder, "folder", "zero.txt")), 0)
        self.assertTrue(os.path.isdir(os.path.join(folder, "folder", "empty")))
        self.assertEqual(self.written, [self.receiving_clipboard.stamp])
        self.assertFalse([name for name in os.listdir(self.inbox) if name.startswith(".incoming")])

    def test_a_local_copy_meanwhile_keeps_the_clipboard(self):
        files = self.sender.offer("peer")
        self.receiver.arrived("peer", protocol.read_files(files))
        self.receiving_clipboard.copy()
        before = self.receiving_clipboard.stamp
        self.sender.send("peer", lambda message: (self.receiver.receive("peer", protocol.parse_message(protocol.dumps_message(message))), True)[1], lambda: 0)
        self.assertTrue(wait_for(lambda: self.notes))
        self.assertEqual(self.receiving_clipboard.stamp, before)
        self.assertEqual(self.written, [])
        self.assertEqual(len([name for name in os.listdir(self.inbox) if not name.startswith(".")]), 1)

    def test_a_piece_out_of_place_discards_everything(self):
        def mangle(message):
            if message.get("type") == protocol.MSG_FILE_DATA and message["data"]["o"] > 0:
                message["data"]["o"] += 1
            return message

        self.carry(mangle)
        self.assertTrue(wait_for(lambda: self.notes))
        time.sleep(0.2)
        self.assertEqual(self.written, [])
        self.assertEqual(os.listdir(self.inbox), [])

    def test_pieces_for_no_transfer_are_dropped(self):
        self.receiver.receive("stranger", protocol.parse_message(protocol.dumps_message(
            protocol.file_data_msg(5, 0, 0, b"x"))))
        self.receiver.receive("stranger", protocol.parse_message(protocol.dumps_message(protocol.file_end_msg(5))))
        time.sleep(0.1)
        self.assertFalse(os.path.exists(self.inbox))

    def test_a_lost_link_stops_the_sender(self):
        files = self.sender.offer("peer")
        self.receiver.arrived("peer", protocol.read_files(files))
        sent = []
        gate = threading.Event()

        def backlog():
            gate.wait(2)
            return 0

        self.sender.send("peer", lambda message: (sent.append(message), True)[1], backlog)
        self.sender.link_lost("peer")
        self.receiver.link_lost("peer")
        gate.set()
        time.sleep(0.2)
        self.assertLessEqual(len(sent), 1)
        self.assertTrue(wait_for(lambda: not os.path.exists(self.inbox) or not os.listdir(self.inbox)))


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


class Failures(unittest.TestCase):
    """What a failed or long transfer tells the user, and that a failure here stops the sender."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="beamer-files-")
        self.addCleanup(lambda: shutil.rmtree(file_transfer._long(self.root), ignore_errors=True))
        self.notes = []
        self.receiving_clipboard = FileClipboard()
        self.receiver = file_transfer.FileShare(lambda: self.receiving_clipboard, written=lambda stamp: None,
                                                notify=self.notes.append,
                                                folder=lambda: os.path.join(self.root, "inbox"))

    def carry(self, paths, sender_notes=None):
        sender = file_transfer.FileShare(lambda: FileClipboard(paths), notify=(sender_notes or []).append)
        files = sender.offer("peer")
        sent = []

        def reply(message):
            sender.receive("peer", protocol.parse_message(protocol.dumps_message(message)))
            return True

        self.receiver.arrived("peer", protocol.read_files(files), reply)

        def post(message):
            sent.append(message["type"])
            self.receiver.receive("peer", protocol.parse_message(protocol.dumps_message(message)))
            return True

        sender.send("peer", post, lambda: 0)
        return sent

    def test_a_folder_deeper_than_windows_paths_go_arrives(self):
        deep = os.path.join(self.root, "project", *(["a_rather_long_folder_name_" + str(n) for n in range(12)]))
        os.makedirs(file_transfer._long(deep))
        target = os.path.join(deep, "asset.uasset")
        self.assertGreater(len(target), 300)
        with open(file_transfer._long(target), "wb") as handle:
            handle.write(b"deep bytes")
        self.carry([os.path.join(self.root, "project")])
        self.assertTrue(wait_for(lambda: self.receiving_clipboard.paths), self.notes)
        landed = os.path.join(self.receiving_clipboard.paths[0], *os.path.relpath(target, os.path.join(self.root, "project")).split(os.sep))
        with open(file_transfer._long(landed), "rb") as handle:
            self.assertEqual(handle.read(), b"deep bytes")
        self.assertFalse(self.receiving_clipboard.paths[0].startswith("\\\\?\\"))

    def test_a_failure_here_says_why_and_stops_the_sender(self):
        source = os.path.join(self.root, "big.bin")
        with open(source, "wb") as handle:
            handle.write(os.urandom(protocol.FILE_CHUNK_BYTES * 40))
        writes = []
        original = file_transfer.FileShare._write

        def failing(share, incoming, index, offset, chunk):
            writes.append(offset)
            if len(writes) == 3:
                raise_into = OSError("disk unplugged")
                share._fail(incoming, raise_into)
                return
            original(share, incoming, index, offset, chunk)

        file_transfer.FileShare._write = failing
        self.addCleanup(setattr, file_transfer.FileShare, "_write", original)
        sent = self.carry([source])
        self.assertTrue(wait_for(lambda: any("disk unplugged" in note for note in self.notes)), self.notes)
        time.sleep(0.3)
        self.assertLess(sent.count(protocol.MSG_FILE_DATA), 40)
        self.assertNotIn(protocol.MSG_FILE_END, sent)

    def test_a_long_transfer_says_it_began_and_how_far_it_got(self):
        source = os.path.join(self.root, "video.mp4")
        with open(source, "wb") as handle:
            handle.write(os.urandom(file_transfer.PROGRESS_MIN_BYTES + 1))
        self.carry([source])
        self.assertTrue(wait_for(lambda: self.receiving_clipboard.paths), self.notes)
        self.assertTrue(self.notes[0].startswith("Receiving 1 files"), self.notes)
        self.assertEqual([note for note in self.notes if "%" in note],
                         [f"Receiving files: {p}% of 8.0 MB" for p in (25, 50, 75)])
        self.assertEqual(self.notes[-1], "Files received: paste to copy them here")


class OverARealLink(unittest.TestCase):
    """The files over a real, sealed link on loopback: to the responder from its owner, and back."""

    def setUp(self):
        from core.tests.responder_harness import B, CAPS, Initiator, Machine, entry
        self.root = tempfile.mkdtemp(prefix="beamer-link-files-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        caps = sorted(set(CAPS) | {file_transfer.CAPABILITY})
        self.machine = Machine([entry(B, "Peer")], caps=caps)
        self.machine.clipboard.read_files = lambda: list(self.machine_files)
        self.machine.clipboard.set_files = self.set_machine_files
        self.machine_files = []
        self.machine.responder._files = file_transfer.FileShare(
            lambda: self.machine.clipboard, written=self.machine.responder.clipboard_written,
            folder=lambda: os.path.join(self.root, "machine-inbox"))
        self.machine.start()
        self.addCleanup(self.machine.stop)
        self.link = Initiator(self.machine)
        self.addCleanup(self.link.close)
        self.link.handshake(self.link.hello(caps=caps))
        self.owner_clipboard = FileClipboard()
        self.owner = file_transfer.FileShare(lambda: self.owner_clipboard,
                                             folder=lambda: os.path.join(self.root, "owner-inbox"))

    def set_machine_files(self, paths):
        self.machine_files = list(paths)
        self.machine.clipboard.stamp += 1
        return True

    def make(self, name, size):
        path = os.path.join(self.root, name)
        data = os.urandom(size)
        with open(path, "wb") as handle:
            handle.write(data)
        return path, data

    def test_the_owner_s_files_land_on_the_machine_it_takes(self):
        path, data = self.make("photo.jpg", 1_500_000)
        route = self.link.take()
        self.assertEqual(self.link.answer(route)[0], protocol.MSG_ACCEPT)
        self.owner_clipboard.paths = [path]
        files = self.owner.offer("machine")
        self.link.send(protocol.clipboard_msg(None, None, files))
        self.owner.send("machine", lambda message: (self.link.send(message), True)[1], lambda: 0)
        self.assertTrue(wait_for(lambda: self.machine_files, 15))
        with open(self.machine_files[0], "rb") as handle:
            self.assertEqual(handle.read(), data)
        # Not sent back when the owner lets go: the change was the owner's own files arriving.
        self.link.let_go()
        self.assertIsNone(self.link.expect(protocol.MSG_CLIPBOARD, timeout=0.5))

    def test_files_copied_on_the_machine_come_back_when_it_is_let_go(self):
        path, data = self.make("report.pdf", 300_000)
        route = self.link.take()
        self.assertEqual(self.link.answer(route)[0], protocol.MSG_ACCEPT)
        self.machine_files = [path]
        self.machine.clipboard.copy("names as text")
        self.link.let_go()
        clipboard = self.link.expect(protocol.MSG_CLIPBOARD, timeout=3)
        self.assertIsNotNone(clipboard)
        self.assertNotIn("text", clipboard)
        self.owner.arrived("machine", protocol.read_files(clipboard["files"]))
        deadline = time.monotonic() + 15
        while not self.owner_clipboard.paths and time.monotonic() < deadline:
            message = self.link._claim(lambda m: m.get("type") in protocol.FILE_TYPES, 0.2)
            if message is not None:
                self.owner.receive("machine", message)
        self.assertTrue(wait_for(lambda: self.owner_clipboard.paths, 5))
        with open(self.owner_clipboard.paths[0], "rb") as handle:
            self.assertEqual(handle.read(), data)

if __name__ == "__main__":
    unittest.main()
