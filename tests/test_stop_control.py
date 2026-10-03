import contextlib
import io
from pathlib import Path
import runpy
import socket
import sys
import threading
import unittest
from unittest.mock import Mock, patch

from proxy_lib.pyto_control import CONTROL_KEY, PytoProxyControl
import stop
import restart


class StopControlTests(unittest.TestCase):
    def setUp(self):
        self.values = {}
        self.storage = Mock()
        self.storage.get.side_effect = lambda key: self.values[key]
        self.storage.set.side_effect = lambda value, key: self.values.update({key: value})
        self.storage.delete.side_effect = lambda key: self.values.pop(key)
        modules = patch.dict(sys.modules, userkeys=self.storage)
        modules.start()
        self.addCleanup(modules.stop)

    def start_control(self):
        stopped = threading.Event()
        control = PytoProxyControl(stopped.set)
        self.addCleanup(control.stop)
        self.assertTrue(control.start())
        self.assertEqual(control.listener.getsockname()[0], "127.0.0.1")
        return control, stopped

    def test_stop_script_authenticates_and_control_socket_is_released(self):
        control, stopped = self.start_control()
        self.assertEqual(stop.CONTROL_KEY, CONTROL_KEY)
        port = self.values[CONTROL_KEY]["port"]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(stop.request_stop())
        self.assertTrue(stopped.wait(1))
        control.stop()
        self.assertFalse(control.thread.is_alive())
        self.assertNotIn(CONTROL_KEY, self.values)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))

    def test_wrong_token_cannot_stop_server_and_later_valid_request_works(self):
        control, stopped = self.start_control()
        with socket.create_connection(control.listener.getsockname(), timeout=1) as client:
            client.sendall(b"wrong token\n")
            self.assertEqual(client.recv(256), b"DENIED\n")
        self.assertFalse(stopped.is_set())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(stop.request_stop())
        self.assertTrue(stopped.wait(1))

    def test_missing_stale_or_invalid_control_exits_without_killing_process(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(stop.request_stop())
            self.values[CONTROL_KEY] = {"port": "not a port", "token": "bad"}
            self.assertFalse(stop.request_stop())
            with socket.socket() as unused:
                unused.bind(("127.0.0.1", 0))
                port = unused.getsockname()[1]
            self.values[CONTROL_KEY] = {"port": port, "token": "a" * 48}
            self.assertFalse(stop.request_stop())

    def test_old_control_cleanup_cannot_remove_new_runs_record(self):
        control, _ = self.start_control()
        newer = {"port": 12345, "token": "b" * 48}
        self.values[CONTROL_KEY] = newer
        control.stop()
        self.assertEqual(self.values[CONTROL_KEY], newer)

    def test_storage_failure_closes_partially_started_listener(self):
        self.storage.set.side_effect = RuntimeError("storage unavailable")
        control = PytoProxyControl(Mock())
        with self.assertLogs(level="ERROR"):
            self.assertFalse(control.start())
        self.assertEqual(control.listener.fileno(), -1)

    def test_without_pyto_server_and_stop_script_leave_host_running(self):
        with patch.dict(sys.modules, userkeys=None):
            control = PytoProxyControl(Mock())
            self.assertFalse(control.start())
            self.assertIsNone(control.listener)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                # Like the Home Screen extension: no proxy package required.
                source = Path(stop.__file__)
                namespace = runpy.run_path(str(source))
            self.assertEqual(namespace["__name__"], "<run_path>")
            self.assertIn("Run stop.py in Pyto", output.getvalue())

    def test_restart_uses_authenticated_command_and_new_run_has_a_new_token(self):
        restarted = threading.Event()
        stopped = threading.Event()
        control = PytoProxyControl(stopped.set, restarted.set)
        self.addCleanup(control.stop)
        self.assertTrue(control.start())
        token = self.values[CONTROL_KEY]["token"]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(restart.request_restart())
            self.assertFalse(restart.request_restart())
        self.assertTrue(restarted.wait(1))
        self.assertFalse(stopped.is_set())
        control.stop()
        replacement, replacement_stopped = self.start_control()
        self.assertNotEqual(token, self.values[CONTROL_KEY]["token"])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(stop.request_stop())
        self.assertTrue(replacement_stopped.wait(1))

    def test_invalid_restart_token_does_not_trigger_either_callback(self):
        stopped = Mock()
        restarted = Mock()
        control = PytoProxyControl(stopped, restarted)
        self.addCleanup(control.stop)
        self.assertTrue(control.start())
        with socket.create_connection(control.listener.getsockname(), timeout=1) as client:
            client.sendall(b"RESTART wrong-token\n")
            self.assertEqual(client.recv(256), b"DENIED\n")
        stopped.assert_not_called()
        restarted.assert_not_called()

    def test_restart_missing_old_or_unavailable_server_leaves_host_running(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(restart.request_restart())
            self.start_control()  # A stop-only server must not be stopped.
            with patch("restart.socket.create_connection") as connect:
                self.assertFalse(restart.request_restart())
                connect.assert_not_called()
            self.values[CONTROL_KEY] = "invalid"
            self.assertFalse(restart.request_restart())
            self.values[CONTROL_KEY] = {"port": 12345, "token": "c" * 48,
                                       "restart_supported": True}
            with patch("restart.socket.create_connection", side_effect=OSError("unreachable")):
                self.assertFalse(restart.request_restart())
        with patch.dict(sys.modules, userkeys=None), contextlib.redirect_stdout(io.StringIO()):
            runpy.run_path(restart.__file__)


if __name__ == "__main__":
    unittest.main()
