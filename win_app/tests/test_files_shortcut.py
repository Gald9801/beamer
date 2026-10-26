"""The files shortcut on this PC's keyboard (core/file_transfer.py): Ctrl+Shift+Alt+V pulls the
files into the folder open where input is, and its V reaches no machine."""

import os
import sys
import time
import unittest

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import explorer_folder
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


class UserWindow(unittest.TestCase):
    """explorer_folder.user_window: which window stands for where the user is when the shortcut
    is pressed, on a fake z-order of (hwnd, class, pid, shown) from the top down."""

    OWN = 100

    def pick(self, foreground, stack):
        order = [hwnd for hwnd, *_ in stack]
        info = {hwnd: (window_class, pid, shown) for hwnd, window_class, pid, shown in stack}

        def next_window(hwnd):
            if not hwnd:
                return order[0] if order else 0
            index = order.index(hwnd) + 1
            return order[index] if index < len(order) else 0

        return explorer_folder.user_window(foreground, next_window, info.__getitem__, own_pid=self.OWN)

    def test_explorer_in_front_is_taken_as_it_is(self):
        stack = [(1, "CabinetWClass", 7, True), (2, "Chrome_WidgetWin_1", 8, True)]
        self.assertEqual(self.pick(1, stack), (1, "CabinetWClass"))

    def test_gameinput_in_front_falls_through_to_the_explorer_under_it(self):
        stack = [(1, "GameInputServiceWindow", 4, True), (2, "CabinetWClass", 7, True)]
        self.assertEqual(self.pick(1, stack), (2, "CabinetWClass"))

    def test_own_overlays_and_hidden_windows_are_skipped_on_the_way_down(self):
        stack = [(1, "Qt6110QWindowToolSaveBits", self.OWN, True), (2, "GameInputServiceWindow", 4, False),
                 (3, "Windows.UI.Core.CoreWindow", 9, False), (4, "Progman", 7, True)]
        self.assertEqual(self.pick(1, stack), (4, "Progman"))

    def test_a_real_window_in_front_is_not_looked_past(self):
        stack = [(1, "Chrome_WidgetWin_1", 8, True), (2, "CabinetWClass", 7, True)]
        self.assertEqual(self.pick(1, stack), (1, "Chrome_WidgetWin_1"))

    def test_the_first_user_window_under_gameinput_decides_even_when_it_is_not_explorer(self):
        stack = [(1, "GameInputServiceWindow", 4, True), (2, "Chrome_WidgetWin_1", 8, True),
                 (3, "CabinetWClass", 7, True)]
        self.assertEqual(self.pick(1, stack), (2, "Chrome_WidgetWin_1"))

    def test_no_foreground_starts_at_the_top(self):
        stack = [(5, "GameInputServiceWindow", 4, True), (6, "CabinetWClass", 7, True)]
        self.assertEqual(self.pick(0, stack), (6, "CabinetWClass"))

    def test_nothing_shown_gives_none(self):
        stack = [(1, "GameInputServiceWindow", 4, True), (2, "CabinetWClass", 7, False)]
        self.assertEqual(self.pick(1, stack), (0, ""))


if __name__ == "__main__":
    unittest.main()
