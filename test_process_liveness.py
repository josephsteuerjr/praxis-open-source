from __future__ import annotations

import os
import unittest
from unittest import mock

import process_liveness


class TestProcessLiveness(unittest.TestCase):
    def test_current_process_is_alive_without_windows_os_kill(self):
        if os.name == "nt":
            with mock.patch.object(process_liveness.os, "kill",
                                   side_effect=AssertionError("os.kill is unsafe on Windows")):
                self.assertTrue(process_liveness.is_process_alive(os.getpid()))
        else:
            self.assertTrue(process_liveness.is_process_alive(os.getpid()))

    def test_invalid_pids_are_not_alive(self):
        for pid in (0, -1, "", None):
            with self.subTest(pid=pid):
                self.assertFalse(process_liveness.is_process_alive(pid))

if __name__ == "__main__":
    unittest.main()
