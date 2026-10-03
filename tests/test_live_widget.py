import contextlib
import io
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from proxy_lib.pyto_control import CONTROL_KEY, PytoProxyControl
from proxy_lib.pyto_widget import InAppWidgetPublisher
import proxy_widget
import stop


class LiveWidgetTests(unittest.TestCase):
    def setUp(self):
        self.values = {}
        self.storage = Mock()
        self.storage.get.side_effect = lambda key: self.values[key]
        self.storage.set.side_effect = lambda value, key: self.values.update({key: value})
        self.storage.delete.side_effect = lambda key: self.values.pop(key)
        modules = patch.dict(sys.modules, userkeys=self.storage)
        modules.start()
        self.addCleanup(modules.stop)

    def test_widget_pulls_current_counters_without_a_saved_traffic_record(self):
        stats = {"state": "running", "host": "127.0.0.1", "connections": 0}
        stopped = threading.Event()
        control = PytoProxyControl(stopped.set, get_status=lambda: dict(stats, updated_at=time.time()))
        self.addCleanup(control.stop)
        self.assertTrue(control.start())
        self.assertEqual(proxy_widget.CONTROL_KEY, CONTROL_KEY)
        self.assertEqual(proxy_widget.read_live_status()["connections"], 0)
        stats["connections"] = 12
        self.assertEqual(proxy_widget.read_live_status()["connections"], 12)
        self.assertEqual({call.args[0] for call in self.storage.get.call_args_list}, {CONTROL_KEY})
        self.assertFalse(stopped.is_set())
        self.assertFalse(control.command_requested)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(stop.request_stop())
        self.assertTrue(stopped.wait(1))

    def test_unavailable_or_wrong_token_does_not_use_cached_running_data(self):
        self.values["ios_socks_server.status.v1"] = {"state": "running", "connections": 99}
        self.assertEqual(proxy_widget.read_live_status()["state"], "unavailable")
        control = PytoProxyControl(Mock(), get_status=lambda: {"state": "running"})
        self.addCleanup(control.stop)
        self.assertTrue(control.start())
        self.values[CONTROL_KEY]["token"] = "0" * 48
        self.assertEqual(proxy_widget.read_live_status()["state"], "unavailable")
        control.stop()
        self.assertEqual(proxy_widget.read_live_status()["state"], "unavailable")

    def test_status_requests_remain_available_during_a_pending_shutdown(self):
        control = PytoProxyControl(Mock(), get_status=lambda: {"state": "running"})
        self.addCleanup(control.stop)
        self.assertTrue(control.start())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(stop.request_stop())
        self.assertEqual(proxy_widget.read_live_status()["state"], "running")

    def test_background_updates_coalesce_save_final_state_and_avoid_previews(self):
        wd = Mock()
        wd.__PyWidget__ = Mock()
        wd.__PyWidget__.alloc.return_value.init.side_effect = lambda: Mock()
        entered = threading.Event()
        release = threading.Event()
        received = []

        def build(wd, data, date):
            received.append(data)
            return SimpleNamespace(**{
                size + "_layout": SimpleNamespace(__widget_view__=Mock())
                for size in ("small", "medium", "large")
            })

        def save(native, key):
            self.assertEqual(key, "iOS Proxy")
            self.assertTrue(native.scriptPath.endswith("proxy_widget.py"))
            entered.set()
            self.assertTrue(release.wait(2))

        wd.__PyWidget__.addWidget.side_effect = save
        worker = InAppWidgetPublisher()
        with patch.dict(sys.modules, widgets=wd), patch("proxy_widget.build_widget", side_effect=build):
            try:
                worker.submit({"state": "starting"})
                self.assertTrue(entered.wait(2))
                worker.submit({"state": "running", "connections": 3})
                worker.submit({"state": "stopped", "connections": 0}, final=True)
                worker.submit({"state": "running", "connections": 4})
            finally:
                release.set()
                worker.wait_closed()
        self.assertFalse(worker.thread.is_alive())
        self.assertEqual([data["state"] for data in received], ["starting", "stopped"])
        wd.save_widget.assert_not_called()
        wd.show_widget.assert_not_called()
        self.assertEqual(wd.__PyWidget__.addWidget.call_count, 2)

    def test_disabled_or_failed_renderer_does_not_affect_server(self):
        disabled = InAppWidgetPublisher(enabled=False)
        disabled.submit({"state": "running"})
        self.assertIsNone(disabled.thread)
        failing = InAppWidgetPublisher()
        wd = Mock()
        with patch.dict(sys.modules, widgets=wd), patch("proxy_widget.build_widget", side_effect=RuntimeError("render failed")):
            with self.assertLogs(level="ERROR"):
                failing.submit({"state": "running"}, final=True)
                failing.wait_closed()
        self.assertFalse(failing.enabled)
        self.assertFalse(failing.thread.is_alive())


if __name__ == "__main__":
    unittest.main()
