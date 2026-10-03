import ast
import asyncio
from datetime import datetime
from pathlib import Path
import logging
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

    def test_widget_timeline_expires_snapshot_without_rereading_storage(self):
        publisher = self.publisher()
        publisher.running()
        wd = Mock()
        wd.link = None
        wd.TimelineProvider = object
        wd.Widget.side_effect = lambda: Mock()
        with patch.dict(sys.modules, widgets=wd):
            widget_main()
        provider = wd.provide_timeline.call_args.args[0]
        dates = provider.timeline()
        self.assertEqual(len(dates), 2)
        for date in dates:
            provider.widget(date)
        texts = [call.args[0] for call in wd.Text.call_args_list]
        self.assertIn("Running (snapshot)", texts)
        self.assertIn("Status unconfirmed", texts)
        self.assertEqual(provider.reload_time().total_seconds(), 0)
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

    def test_standalone_widget_reads_the_servers_shared_status_and_expiry(self):
        publisher = self.publisher()
        publisher.running()
        self.assertEqual(proxy_widget.STATUS_KEY, STATUS_KEY)
        self.assertEqual(proxy_widget.STALE_AFTER, STALE_AFTER)
        self.assertEqual(proxy_widget.read_status(), read_status())
        snapshot = read_status()
        for age in (0, STALE_AFTER - 1, STALE_AFTER, STALE_AFTER + 1):
            now = snapshot["updated_at"] + age
            self.assertEqual(proxy_widget.display_state(snapshot, now),
                             display_state(snapshot, now))

    def run_script(self, *, wifi=False, fail_start=False, native_stop=False):
        """Exercise run() without iOS interface detection or real listeners."""
        path = Path(__file__).resolve().parents[1] / "socks5.py"
        tree = ast.parse(path.read_text())
        run_node = next(node for node in tree.body
                        if isinstance(node, ast.FunctionDef) and node.name == "run")
        watcher = Mock()
        watcher.start.return_value = False
        servers = []

        def make_server(*args, **kwargs):
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
                if wifi or fail_start:
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
            if not wifi:
                raise KeyboardInterrupt

        watcher_factory = Mock(return_value=watcher)
        namespace = dict(
            asyncio=asyncio, logging=logging, threading=threading,
            BackgroundAudio=Mock(), PytoStopWatcher=watcher_factory,
            PytoStatusPublisher=PytoStatusPublisher, cleanup_steps=cleanup_steps,
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
        exec(compile(ast.Module(body=[run_node], type_ignores=[]), str(path), "exec"), namespace)
        namespace["run"]()
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


if __name__ == "__main__":
    unittest.main()
