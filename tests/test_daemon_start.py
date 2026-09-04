import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch
from contextlib import ExitStack

from cloak_browse import cli


class DaemonStartTests(unittest.TestCase):
    def run_start(self, *, alive, ticks, exited=None):
        proc = Mock(pid=123, returncode=exited)
        proc.poll.return_value = exited
        ipc = ModuleType('browser_harness._ipc')
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            ipc.log_path = lambda name: Path(directory) / 'missing.log'
            stack.enter_context(patch.dict('sys.modules', {'browser_harness._ipc': ipc}))
            stack.enter_context(patch.object(cli, 'IS_WINDOWS', False))
            stack.enter_context(patch.object(cli.subprocess, 'Popen', return_value=proc))
            stack.enter_context(patch.object(cli.time, 'monotonic', side_effect=ticks))
            pause = stack.enter_context(patch.object(cli.time, 'sleep'))
            stack.enter_context(patch.object(cli, '_bh_daemon_alive', side_effect=alive))
            stack.enter_context(patch.object(cli, '_identify_bh_daemon', return_value=123))
            stack.enter_context(patch.object(cli, '_process_start_identity', return_value='start:123'))
            save = stack.enter_context(patch.object(cli, '_save_session'))
            kill = stack.enter_context(patch.object(cli, '_kill_pid'))
            result = cli._start_bh_daemon(stop_existing=False)
            return result, pause, save, kill

    def test_readiness_after_old_fifteen_second_deadline(self):
        result, _, save, kill = self.run_start(alive=[False, True], ticks=[0, 1, 20])
        self.assertTrue(result)
        save.assert_called_once_with(daemonPid=123, daemonStartIdentity='start:123')
        kill.assert_not_called()

    def test_early_exit_does_not_wait_for_deadline(self):
        result, pause, _, kill = self.run_start(alive=[], ticks=[0, 1], exited=7)
        self.assertFalse(result)
        pause.assert_not_called()
        kill.assert_called_once_with(123)

    def test_timeout_reaps_owned_process(self):
        result, _, save, kill = self.run_start(alive=[False], ticks=[0, 1, 91])
        self.assertFalse(result)
        save.assert_not_called()
        kill.assert_called_once_with(123)
