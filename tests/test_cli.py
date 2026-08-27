import unittest
from unittest.mock import Mock, patch

from cloak_browse import cli


class Utf8OutputTests(unittest.TestCase):
    def test_force_utf8_output_reconfigures_both_streams(self):
        stdout = Mock()
        stderr = Mock()

        with patch.object(cli.sys, "stdout", stdout), patch.object(cli.sys, "stderr", stderr):
            cli._force_utf8_output()

        stdout.reconfigure.assert_called_once_with(
            encoding="utf-8", errors="backslashreplace"
        )
        stderr.reconfigure.assert_called_once_with(
            encoding="utf-8", errors="backslashreplace"
        )

    def test_worker_configures_utf8_before_executing_code(self):
        with patch.object(cli, "_force_utf8_output") as force_utf8:
            cli._run_code_child("worker_value = 1", {})

        force_utf8.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
