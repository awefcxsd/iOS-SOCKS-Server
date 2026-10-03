import ast
import asyncio
from datetime import datetime
from pathlib import Path
import logging
import runpy
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

from proxy_lib.pyto_status import (
    HEARTBEAT_INTERVAL, STALE_AFTER, STATUS_KEY, PytoStatusPublisher,
    display_state, read_status,
)
from proxy_lib.status import StatusMonitor
from proxy_lib.lifecycle import cleanup_steps
from proxy_widget import build_widget, main as widget_main
import proxy_widget


class PytoStatusTests(unittest.TestCase):
    def setUp(self):
        self.saved = {}
        self.userkeys = Mock()
        self.userkeys.set.side_effect = lambda value, key: self.saved.update({key: value})
        self.userkeys.get.side_effect = lambda key: self.saved[key]
        self.notifications = Mock()
        self.modules = patch.dict(sys.modules, userkeys=self.userkeys,
                                  notifications=self.notifications)
        self.modules.start()
        self.addCleanup(self.modules.stop)
        widget_publisher = patch("proxy_lib.pyto_status.InAppWidgetPublisher")
        widget_publisher.start()
        self.addCleanup(widget_publisher.stop)

    def publisher(self, **kwargs):
        return PytoStatusPublisher("192.168.1.2", 9876, 9877, 8088, **kwargs)

    def test_heartbeat_and_final_totals_with_single_shutdown_notification(self):
        publisher = self.publisher()
        publisher.starting()
        self.assertEqual(read_status()["state"], "starting")
        self.notifications.send_notification.assert_not_called()
        publisher.running()
        stats = StatusMonitor("test")
        stats.add_connection()
        stats.add_inbound(120)
        stats.add_outbound(45)
        publisher.last_publish -= HEARTBEAT_INTERVAL
        publisher.update(stats)
        self.assertEqual(read_status()["connections"], 1)
        self.assertEqual(read_status()["in_bytes"], 120)
        writes = self.userkeys.set.call_count
        publisher.update(stats)
        self.assertEqual(self.userkeys.set.call_count, writes)
        stats.add_inbound(10)
        publisher.finish("WiFi disconnected", stats=stats)
        publisher.finish("Stopped by user")
        publisher.running()
        publisher.update(stats)
        final = read_status()
        self.assertEqual(final["state"], "stopped")
        self.assertEqual(final["reason"], "WiFi disconnected")
        self.assertEqual(final["connections"], 0)
        self.assertEqual(final["in_bytes"], 130)
        self.assertEqual(self.notifications.send_notification.call_count, 2)

    def test_failure_alert_and_missing_or_corrupt_storage(self):
        self.assertEqual(read_status(), {})
        self.saved[STATUS_KEY] = "bad data"
        self.assertEqual(read_status(), {})
        publisher = self.publisher()
        publisher.finish("Address already in use", failed=True)
        self.assertEqual(read_status()["state"], "failed")
        self.assertIn("Proxy failed", self.notifications.Notification.call_args.kwargs["message"])

    def test_unavailable_apis_and_notification_failure_do_not_stop_proxy(self):
        with patch.dict(sys.modules, userkeys=None, notifications=None):
            publisher = self.publisher()
            publisher.starting()
            publisher.running()
            publisher.finish()
        self.notifications.send_notification.side_effect = RuntimeError("permission denied")
        with self.assertLogs(level="WARNING"):
            publisher = self.publisher()
            publisher.running()
            publisher.finish()
        self.assertEqual(read_status()["state"], "stopped")

    def test_feature_flags_disable_status_apis(self):
        publisher = self.publisher(notifications_enabled=False, widget_enabled=False)
        publisher.starting()
        publisher.running()
        publisher.finish()
        self.userkeys.set.assert_not_called()
        self.notifications.send_notification.assert_not_called()

    def test_old_heartbeat_is_unconfirmed_but_stopped_status_remains_stopped(self):
        now = 1000
        snapshot = {"state": "running", "updated_at": now}
        self.assertEqual(display_state(snapshot, now + STALE_AFTER - 1), "running")
        self.assertEqual(display_state(snapshot, now + STALE_AFTER), "stale")
        self.assertEqual(display_state(snapshot, now - 2), "stale")
        self.assertEqual(display_state({"state": "running"}, now), "unknown")
        snapshot["state"] = "stopped"
        self.assertEqual(display_state(snapshot, now + 10000), "stopped")

    def test_widget_launcher_saves_named_in_app_content_from_live_data(self):
        data = {"state": "running", "updated_at": datetime.now().timestamp(),
                "host": "192.168.1.2", "connections": 7, "in_bytes": 1234}
        wd = Mock()
        wd.link = None
        wd.Widget.side_effect = lambda: Mock()
        with patch.dict(sys.modules, widgets=wd), patch("proxy_widget.read_live_status", return_value=data):
            widget_main()
        wd.save_widget.assert_called_once()
        self.assertEqual(wd.save_widget.call_args.args[1], "iOS Proxy")
        wd.provide_timeline.assert_not_called()
        texts = [call.args[0] for call in wd.Text.call_args_list]
        self.assertIn("Running", texts)
        self.assertIn("7 connections", texts)
        build_widget(wd, {}, datetime.now())
        self.assertIn("No status yet", [call.args[0] for call in wd.Text.call_args_list])

    def test_widget_runs_as_a_standalone_file_without_project_imports(self):
        with tempfile.TemporaryDirectory() as directory:
            standalone = Path(directory) / "proxy_widget.py"
            standalone.write_text(Path(proxy_widget.__file__).read_text())
            result = subprocess.run(
                [sys.executable, "-I", str(standalone)], cwd=directory,
                capture_output=True, text=True, timeout=10,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("This widget requires Pyto", result.stdout)

    def test_home_screen_run_path_draws_widgets_and_tap_fallback_draws_too(self):
        # Pyto's extension calls runpy.run_path(path) without run_name; its
        # fallback tap URL executes the code with __name__ set to "widget".
        wd = Mock()
        wd.link = None
        wd.TimelineProvider = object
        wd.Widget.side_effect = lambda: Mock()
        with patch.dict(sys.modules, widgets=wd):
            namespace = runpy.run_path(proxy_widget.__file__)
            self.assertEqual(namespace["__name__"], "<run_path>")
            wd.save_widget.assert_called_once()
            self.assertIn("iOS Proxy", [call.args[0] for call in wd.Text.call_args_list])
            wd.save_widget.reset_mock()
            runpy.run_path(proxy_widget.__file__, run_name="widget")
            wd.save_widget.assert_called_once()

    def test_live_status_uses_counters_newer_than_the_persisted_heartbeat(self):
        publisher = self.publisher()
        publisher.running()
        stats = StatusMonitor("test")
        stats.add_connection()
        stats.add_inbound(42)
        live = publisher.live_status(stats)
        self.assertEqual(live["connections"], 1)
        self.assertEqual(live["in_bytes"], 42)
        self.assertEqual(read_status()["connections"], 0)
        self.assertEqual(read_status()["in_bytes"], 0)
        self.assertEqual(proxy_widget.STALE_AFTER, STALE_AFTER)
        publisher.finish(stats=stats)
        self.assertEqual(publisher.live_status(stats)["connections"], 0)

    def run_script(self, *, wifi=False, fail_start=False, native_stop=False,
                   stop_script=False, restart_script=False):
        """Exercise run() without iOS interface detection or real listeners."""
        path = Path(__file__).resolve().parents[1] / "socks5.py"
        tree = ast.parse(path.read_text())
        run_node = next(node for node in tree.body
                        if isinstance(node, ast.FunctionDef) and node.name == "run")
        entry_node = next(node for node in tree.body
                          if isinstance(node, ast.FunctionDef) and node.name == "run_proxy")
        watcher = Mock()
        watcher.start.return_value = False
        servers = []

        def make_server(*args, **kwargs):
            if restart_script and len(servers) == 2:
                # The replacement must not start until old coroutine cleanup
                # has completed; otherwise fixed listener ports can collide.
                for previous in servers:
                    previous.close.assert_awaited_once()
                    previous.stop_now.assert_called()
            server = Mock()
            server.start = AsyncMock()
            server.close = AsyncMock()
            if fail_start and servers:
                server.start.side_effect = OSError("port occupied")
            servers.append(server)
            return server

        def make_thread(**kwargs):
            thread = Mock()
            if kwargs.get("name") == "wifi-disconnect-monitor":
                thread.start.side_effect = kwargs["args"][-1]
            return thread

        def run_until(coro, stop):
            async def scenario():
                if wifi or fail_start or stop_script or restart_script:
                    await coro
                    return
                task = asyncio.create_task(coro)
                try:
                    for _ in range(100):
                        if read_status().get("state") == "running":
                            break
                        await asyncio.sleep(0)
                    else:
                        self.fail("proxy did not start")
                    if native_stop:
                        watcher_factory.call_args.args[0]()
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            asyncio.run(scenario())
            if not wifi and not stop_script and not restart_script:
                raise KeyboardInterrupt

        watcher_factory = Mock(return_value=watcher)
        control_factory = Mock()
        control_factory.return_value.start.return_value = False
        if stop_script:
            control_factory.return_value.start.side_effect = lambda: control_factory.call_args.args[0]()
        if restart_script:
            def request_restart_then_stop():
                callbacks = control_factory.call_args.args
                callbacks[1 if control_factory.call_count == 1 else 0]()
            control_factory.return_value.start.side_effect = request_restart_then_stop
        namespace = dict(
            asyncio=asyncio, logging=logging, threading=threading,
            BackgroundAudio=Mock(), PytoStopWatcher=watcher_factory,
            PytoStatusPublisher=PytoStatusPublisher, cleanup_steps=cleanup_steps,
            PytoProxyControl=control_factory,
            create_wpad_server=Mock(), stop_wpad_server=Mock(),
            run_wpad_server=Mock(), service_thread=make_thread,
            run_until_stopped=run_until, StatusMonitor=StatusMonitor,
            AsyncProxyServer=make_server, AsyncSocks5Handler=Mock(),
            AsyncHTTPProxyHandler=Mock(), PROXY_HOST="192.168.1.2",
            LISTEN_HOST="0.0.0.0", SOCKS_PORT=9876, HTTP_PORT=9877, WPAD_PORT=8088,
            ENABLE_STATUS_NOTIFICATIONS=True, ENABLE_STATUS_WIDGET=True,
            BACKGROUND_AUDIO_TEST_TONE=False, KEEP_ALIVE_WITH_AUDIO=False,
            initial_output="", EXIT_ON_WIFI_DISCONNECT=wifi,
            wifi_interface_name="en0", wifi_interface_address="192.168.1.2",
            WIFI_NETWORK_NAME="Test WiFi", WIFI_CHECK_INTERVAL=2,
            REFRESH_SOURCE_ADDRESSES=False, connect_interface_ipv4=None,
            CONNECT_HOST_IPV4=None, resolver=None, monitor_wifi_connection=Mock(),
        )
        exec(compile(ast.Module(body=[run_node, entry_node], type_ignores=[]), str(path), "exec"), namespace)
        namespace["run_proxy"]()
        return servers

    def test_normal_stop_publishes_once_after_listener_start_and_closes_services(self):
        servers = self.run_script()
        self.assertEqual(read_status()["state"], "stopped")
        self.assertEqual(self.notifications.send_notification.call_count, 2)
        for server in servers:
            server.stop_now.assert_called()
            server.close.assert_awaited()

    def test_wifi_shutdown_reason_survives_repeated_cleanup(self):
        self.run_script(wifi=True)
        self.assertEqual(read_status()["reason"], "WiFi network Test WiFi disconnected")
        self.assertEqual(self.notifications.send_notification.call_count, 2)

    def test_partial_startup_failure_sends_only_failure_alert(self):
        with self.assertRaisesRegex(OSError, "port occupied"):
            self.run_script(fail_start=True)
        self.assertEqual(read_status()["state"], "failed")
        self.assertEqual(self.notifications.send_notification.call_count, 1)

    def test_native_stop_publishes_before_parked_script_cleanup(self):
        servers = self.run_script(native_stop=True)
        self.assertEqual(read_status()["reason"], "Stopped with Pyto's Stop button")
        self.assertEqual(self.notifications.send_notification.call_count, 2)
        for server in servers:
            server.emergency_stop.assert_called_once()

    def test_stop_script_requests_normal_cleanup_without_interrupting_pyto(self):
        servers = self.run_script(stop_script=True)
        self.assertEqual(read_status()["reason"], "Stopped by stop.py")
        self.assertEqual(read_status()["state"], "stopped")
        self.assertEqual(self.notifications.send_notification.call_count, 2)
        for server in servers:
            server.stop_now.assert_called()
            server.close.assert_awaited()
            server.emergency_stop.assert_not_called()

    def test_restart_finishes_cleanup_before_starting_a_fresh_run(self):
        servers = self.run_script(restart_script=True)
        self.assertEqual(len(servers), 4)
        for server in servers:
            server.close.assert_awaited_once()
            server.emergency_stop.assert_not_called()
        messages = [call.kwargs["message"] for call in self.notifications.Notification.call_args_list]
        self.assertEqual(len(messages), 4)
        self.assertIn("Proxy started", messages[0])
        self.assertIn("Restart requested by restart.py", messages[1])
        self.assertIn("Proxy started", messages[2])
        self.assertIn("Stopped by stop.py", messages[3])
        self.assertEqual(read_status()["state"], "stopped")


if __name__ == "__main__":
    unittest.main()
