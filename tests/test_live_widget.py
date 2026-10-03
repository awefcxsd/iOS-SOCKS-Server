import contextlib
import io
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from proxy_lib.pyto_control import CONTROL_KEY, PytoProxyControl
from proxy_lib.pyto_widget import InAppWidgetPublisher, WIDGET_REFRESH_INTERVAL
from proxy_lib.pyto_status import HEARTBEAT_INTERVAL, PytoStatusPublisher
import proxy_widget
import stop
import restart
from queue import Empty


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
        self.assertEqual(proxy_widget.read_live_status(retry_timeout=0)["state"], "unavailable")
        control = PytoProxyControl(Mock(), get_status=lambda: {"state": "running"})
        self.addCleanup(control.stop)
        self.assertTrue(control.start())
        self.values[CONTROL_KEY]["token"] = "0" * 48
        self.assertEqual(proxy_widget.read_live_status(retry_timeout=0)["state"], "unavailable")
        control.stop()
        self.assertEqual(proxy_widget.read_live_status(retry_timeout=0)["state"], "unavailable")

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
        self.assertTrue(failing.enabled)
        self.assertFalse(failing.thread.is_alive())

    def test_widget_survives_module_eviction_and_continues_publishing_after_restart(self):
        wd = Mock()
        wd.__PyWidget__ = Mock()
        wd.__PyWidget__.alloc.return_value.init.side_effect = lambda: Mock()
        first_saved = threading.Event()
        stopped_saved = threading.Event()
        replacement_saved = threading.Event()
        updated_saved = threading.Event()
        entries = []

        def build(wd, data, date):
            return SimpleNamespace(**{
                size + "_layout": SimpleNamespace(__widget_view__=dict(data))
                for size in ("small", "medium", "large")
            })

        def save(native, key):
            data = native.addView.call_args_list[0].args[0]
            entries.append(data)
            if data["host"] == "first":
                (first_saved if data["state"] == "running" else stopped_saved).set()
            elif data["state"] == "running":
                (updated_saved if data["connections"] == 3 else replacement_saved).set()

        wd.__PyWidget__.addWidget.side_effect = save
        worker = InAppWidgetPublisher(refresh_interval=0.02)
        active = []
        with patch.dict(sys.modules, widgets=wd), patch("proxy_widget.build_widget", side_effect=build), \
                patch("proxy_widget.read_live_status", side_effect=lambda **kwargs: active[0].live_status()):
            first = PytoStatusPublisher("first", 1, 2, 3, notifications_enabled=False,
                                        widget_publisher=worker)
            active.append(first)
            first.running()
            self.assertTrue(first_saved.wait(2))
            original_thread = worker.thread
            first.finish("Restart requested", restarting=True)
            self.assertTrue(stopped_saved.wait(2))
            self.assertFalse(worker.finished)
            # Pyto drops/reimports these modules when restart.py runs. Existing
            # working bindings must stay in the retained worker, even if a new
            # import would return incompatible UI classes.
            with patch.dict(sys.modules, widgets=SimpleNamespace(), proxy_widget=SimpleNamespace()):
                second = PytoStatusPublisher("second", 1, 2, 3, notifications_enabled=False,
                                             widget_publisher=worker)
                active[0] = second
                try:
                    second.running()
                    self.assertTrue(replacement_saved.wait(2))
                    second.last_publish -= HEARTBEAT_INTERVAL
                    second.update(SimpleNamespace(snapshot=lambda: {"connections": 3}))
                    self.assertTrue(updated_saved.wait(2))
                    self.assertIs(worker.thread, original_thread)
                finally:
                    second.finish("Stopped by user")
        self.assertFalse(worker.thread.is_alive())
        self.assertEqual(entries[-1]["host"], "second")
        self.assertEqual(entries[-1]["state"], "stopped")

    def test_live_lookup_rediscovers_replacement_control_after_restart_gap(self):
        requested = threading.Event()
        first_lookup = threading.Event()
        gap = threading.Event()
        completed = threading.Event()
        old = PytoProxyControl(Mock(), requested.set,
                               get_status=lambda: {"state": "running", "host": "old"})
        new = PytoProxyControl(Mock(), get_status=lambda: {"state": "running", "host": "new"})
        self.addCleanup(old.stop)
        self.addCleanup(new.stop)
        self.assertTrue(old.start())
        old_token = self.values[CONTROL_KEY]["token"]

        def replace():
            if not requested.wait(2):
                return
            old.stop()
            gap.set()
            if first_lookup.wait(2):
                new.start()
                completed.set()

        lifecycle = threading.Thread(target=replace, daemon=True)
        lifecycle.start()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(restart.request_restart())
        self.assertTrue(gap.wait(2))

        def get(key):
            if key == CONTROL_KEY and key not in self.values:
                first_lookup.set()
                raise KeyError(key)
            return self.values[key]

        self.storage.get.side_effect = get
        data = proxy_widget.read_live_status(retry_timeout=2)
        lifecycle.join(timeout=2)
        self.assertTrue(completed.is_set())
        self.assertEqual(data["state"], "running")
        self.assertEqual(data["host"], "new")
        self.assertNotEqual(self.values[CONTROL_KEY]["token"], old_token)

    def test_render_error_is_retried_on_the_next_update(self):
        failed = threading.Event()
        wd = Mock()
        wd.__PyWidget__ = Mock()
        widget = SimpleNamespace(**{
            size + "_layout": SimpleNamespace(__widget_view__=Mock())
            for size in ("small", "medium", "large")
        })
        calls = []

        def render(wd, data, date):
            calls.append(data)
            if len(calls) == 1:
                failed.set()
                raise RuntimeError("temporary bridge failure")
            return widget

        worker = InAppWidgetPublisher()
        with patch.dict(sys.modules, widgets=wd), patch("proxy_widget.build_widget", side_effect=render):
            with self.assertLogs(level="ERROR"):
                worker.submit({"state": "running"})
                self.assertTrue(failed.wait(2))
                worker.submit({"state": "stopped"}, final=True)
                worker.wait_closed()
        self.assertTrue(worker.enabled)
        self.assertFalse(worker.thread.is_alive())
        wd.__PyWidget__.addWidget.assert_called_once()

    def test_polling_fetches_changed_live_counters_without_server_heartbeat_pushes(self):
        self.assertEqual(WIDGET_REFRESH_INTERVAL, 10)
        data = {"state": "running", "connections": 0, "host": "127.0.0.1"}
        control = PytoProxyControl(Mock(), get_status=lambda: dict(data, updated_at=time.time()))
        self.addCleanup(control.stop)
        self.assertTrue(control.start())
        wd = Mock()
        wd.__PyWidget__ = Mock()
        wd.__PyWidget__.alloc.return_value.init.side_effect = lambda: Mock()
        initial_saved = threading.Event()
        live_saved = threading.Event()

        def build(wd, values, date):
            return SimpleNamespace(**{
                size + "_layout": SimpleNamespace(__widget_view__=dict(values))
                for size in ("small", "medium", "large")
            })

        def save(native, key):
            values = native.addView.call_args_list[0].args[0]
            if values.get("connections") == 11:
                live_saved.set()
            else:
                initial_saved.set()

        wd.__PyWidget__.addWidget.side_effect = save
        worker = InAppWidgetPublisher(refresh_interval=0.02)
        with patch.dict(sys.modules, widgets=wd), patch("proxy_widget.build_widget", side_effect=build):
            try:
                worker.submit(data)
                self.assertTrue(initial_saved.wait(2))
                # No submit/update call: only the live server counters change.
                data["connections"] = 11
                self.assertTrue(live_saved.wait(2))
            finally:
                worker.submit({"state": "stopped"}, final=True)
                worker.wait_closed()
        self.assertFalse(worker.thread.is_alive())

    def test_polling_cadence_accounts_for_time_spent_rendering(self):
        clock = [0]
        waits = []
        fetched = []
        wd = Mock()
        wd.__PyWidget__ = Mock()
        widget = SimpleNamespace(**{
            size + "_layout": SimpleNamespace(__widget_view__=Mock())
            for size in ("small", "medium", "large")
        })
        worker = InAppWidgetPublisher()
        worker.thread = SimpleNamespace(script_path="proxy_widget.py")
        worker.jobs = Mock()

        def wait(timeout):
            waits.append(timeout)
            if len(waits) == 3:
                return {"state": "stopped"}, True
            clock[0] += timeout
            raise Empty

        def read(**kwargs):
            fetched.append(clock[0])
            return {"state": "running"}

        def build(*args):
            clock[0] += 2  # Simulate slow native rendering.
            return widget

        worker.jobs.get.side_effect = wait
        with patch.dict(sys.modules, widgets=wd), \
                patch("proxy_widget.read_live_status", side_effect=read), \
                patch("proxy_widget.build_widget", side_effect=build), \
                patch("proxy_lib.pyto_widget.time.monotonic", side_effect=lambda: clock[0]):
            worker._publish()
        self.assertEqual(fetched, [10, 20])
        self.assertEqual(waits, [10, 8, 8])


if __name__ == "__main__":
    unittest.main()
