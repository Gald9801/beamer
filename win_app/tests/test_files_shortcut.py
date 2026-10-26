"""The files shortcut on this PC's keyboard (core/file_transfer.py): Ctrl+Shift+Alt+V pulls the
files into the folder open where input is, and its V reaches no machine."""

import os
import sys
import time
import unittest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core import protocol
from links_rig import B, CAPS, Rig

CTRL, SHIFT, ALT, V = 0xA2, 0xA0, 0xA4, 0x56


class FakeFiles:
    def __init__(self):
        self.pulls = 0

    def pull(self, folder=None):
        self.pulls += 1
        return True

    def offer(self, peer, post, backlog, name=None):
        return None

    def link_lost(self, peer):
        pass


def wait_for(condition, seconds=2.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


class FilesShortcut(unittest.TestCase):
    def setUp(self):
        self.rig = Rig()
        self.files = FakeFiles()
        self.rig.sender._files = self.files
        self.rig.links.caps_of[B] = frozenset(CAPS) | {"clipboard_files"}

    def chord(self):
        sender = self.rig.sender
        results = [sender.on_key("ctrl", True, CTRL), sender.on_key("shift", True, SHIFT),
                   sender.on_key("alt", True, ALT)]
        results.append(sender.on_key("v", True, V))
        results.append(sender.on_key("v", False, V))
        for name, vk in (("alt", ALT), ("shift", SHIFT), ("ctrl", CTRL)):
            sender.on_key(name, False, vk)
        return results

    def test_at_home_it_pulls_here_and_v_stays_out_of_every_app(self):
        results = self.chord()
        self.assertEqual(results[3:], [True, True])
        self.assertTrue(wait_for(lambda: self.files.pulls == 1))

    def test_driving_another_machine_it_asks_that_machine_to_pull(self):
        self.rig.push(4)
        self.rig.accept_take()
        self.assertEqual(self.rig.sender._owner.on, B)
        results = self.chord()
        self.assertEqual(results[3:], [True, True])
        self.assertTrue(wait_for(lambda: self.rig.sent(B, protocol.MSG_FILE_PULL)))
        keys = [message["data"].get("key") for message in self.rig.sent(B) if message["type"] in (protocol.MSG_KEYDOWN, protocol.MSG_KEYUP)]
        self.assertNotIn("v", keys)
        self.assertEqual(self.files.pulls, 0)

    def test_ctrl_shift_v_alone_is_still_an_ordinary_key(self):
        sender = self.rig.sender
        sender.on_key("ctrl", True, CTRL)
        sender.on_key("shift", True, SHIFT)
        self.assertFalse(sender.on_key("v", True, V))
        time.sleep(0.05)
        self.assertEqual(self.files.pulls, 0)


if __name__ == "__main__":
    unittest.main()
