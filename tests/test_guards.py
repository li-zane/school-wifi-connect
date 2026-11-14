import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock

from njust_wifi.probes import ApplicationGuards


class GuardTests(unittest.TestCase):
    def test_running_guard_updates_health_without_new_action_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / 'sample.exe'
            executable.touch()
            guard = ApplicationGuards(root)
            guard.paths = [executable]
            event, health = Mock(), Mock()
            result = Mock(returncode=0, stdout=b'"sample.exe","123"\r\n')
            with patch('njust_wifi.probes.subprocess.run', return_value=result):
                guard.check(threading.Event(), event, health)
                guard.check(threading.Event(), event, health)
            event.assert_not_called()
            self.assertEqual(health.call_count, 2)
            self.assertTrue(health.call_args.args[-1])

    def test_each_real_restart_attempt_is_recorded_even_if_previous_start_was_requested(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / 'sample.exe'
            executable.touch()
            guard = ApplicationGuards(root)
            guard.paths = [executable]
            event, health = Mock(), Mock()
            results = [Mock(returncode=0, stdout=b'INFO: no tasks\r\n'), Mock(returncode=0), Mock(returncode=0)] * 2
            with patch('njust_wifi.probes.subprocess.run', side_effect=results):
                guard.check(threading.Event(), event, health)
                guard.check(threading.Event(), event, health)
            self.assertEqual(sum('正在启动' in call.args[1] for call in event.call_args_list), 2)
            self.assertTrue(all(not call.args[-1] for call in health.call_args_list))

    def test_unconfigured_release_guard_is_idle(self):
        with tempfile.TemporaryDirectory() as directory, patch("njust_wifi.probes.subprocess.run") as run:
            ApplicationGuards(Path(directory)).check(threading.Event(), Mock())
            run.assert_not_called()

    def test_running_application_is_not_started_again(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "sample.exe"
            executable.touch()
            (root / "private-probes.json").write_text(json.dumps([str(executable)]))
            result = Mock(returncode=0, stdout=b'"sample.exe","123","Console","1","100 K"\r\n')
            with patch("njust_wifi.probes.subprocess.run", return_value=result) as run, patch("njust_wifi.probes.subprocess.Popen") as launch:
                ApplicationGuards(root).check(threading.Event(), Mock())
                self.assertEqual(run.call_count, 1)
                launch.assert_not_called()

    def test_stopped_application_uses_existing_elevated_task_without_logging_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "sample.exe"
            executable.touch()
            (root / "private-probes.json").write_text(json.dumps([str(executable)]))
            results = [Mock(returncode=0, stdout=b'INFO: no tasks\r\n'), Mock(returncode=0), Mock(returncode=0)]
            events = Mock()
            with patch("njust_wifi.probes.subprocess.run", side_effect=results) as run:
                ApplicationGuards(root).check(threading.Event(), events)
                self.assertEqual(run.call_args.args[0][1], "/Run")
                self.assertNotIn(str(root), str(events.call_args))

