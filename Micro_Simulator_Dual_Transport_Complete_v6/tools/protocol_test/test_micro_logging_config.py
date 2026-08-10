#!/usr/bin/env python3
"""Regression check for the simulator's deferred-console logger capacity."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


class LoggingConfigTests(unittest.TestCase):
    def test_deferred_logger_buffer_has_console_burst_headroom(self) -> None:
        config = Path(__file__).resolve().parents[2] / "applications" / "micro_simulator" / "prj.conf"
        text = config.read_text(encoding="utf-8")
        match = re.search(r"^CONFIG_LOG_BUFFER_SIZE=(\d+)\s*$", text, re.MULTILINE)
        self.assertIsNotNone(match, "CONFIG_LOG_BUFFER_SIZE must be explicitly configured")
        self.assertGreaterEqual(int(match.group(1)), 16384)


if __name__ == "__main__":
    unittest.main(verbosity=2)
