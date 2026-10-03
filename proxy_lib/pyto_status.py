"""Pyto lifecycle notifications, live status, and In App widget updates."""

import logging
import time
from proxy_lib.pyto_widget import InAppWidgetPublisher


STATUS_KEY = "ios_socks_server.status.v1"
HEARTBEAT_INTERVAL = 10
STALE_AFTER = 60


def read_status():
    """Read app-group storage without importing or starting the proxy."""
    try:
        import userkeys
        value = userkeys.get(STATUS_KEY)
        return value if isinstance(value, dict) else {}
    except (ImportError, KeyError):
        return {}
    except Exception as error:
        logging.warning("Could not read Pyto proxy status: %s", error)
        return {}


def display_state(snapshot, now=None):
    state = snapshot.get("state", "unknown")
    if state in ("starting", "running"):
        try:
            age = (time.time() if now is None else now) - float(snapshot["updated_at"])
            # datetime timestamps round to microseconds; tolerate tiny rounding
            # differences, while treating a large backward clock jump as stale.
            if age < -1 or age >= STALE_AFTER:
                return "stale"
        except (KeyError, TypeError, ValueError):
            return "unknown"
    return state


class PytoStatusPublisher:
    def __init__(self, host, socks_port, http_port, wpad_port,
                 notifications_enabled=True, widget_enabled=True, widget_publisher=None):
        self.notifications_enabled = notifications_enabled
        self.widget_enabled = widget_enabled
        self.storage_enabled = widget_enabled
        self.widget_publisher = (widget_publisher if widget_publisher is not None
                                 else InAppWidgetPublisher(enabled=widget_enabled))
        self.snapshot = {
            "state": "starting", "reason": "Starting proxy",
            "host": host, "socks_port": socks_port, "http_port": http_port,
            "pac_url": f"http://{host}:{wpad_port}/wpad.dat",
            "connections": 0, "in_mbps": 0, "out_mbps": 0,
            "in_bytes": 0, "out_bytes": 0, "errors": 0,
        }
        self.finished = False
        self.last_publish = 0

    def _publish(self, final=False, refresh_widget=True):
        self.snapshot["updated_at"] = time.time()
        self.last_publish = time.monotonic()
        if not self.widget_enabled:
            return
        if refresh_widget:
            self.widget_publisher.submit(self.snapshot, final=final)
        if not self.storage_enabled:
            return
        try:
            import userkeys
            # Pyto's argument order is value, key. Its app-group defaults are
            # accessible in both the app and the separate widget process.
            userkeys.set(dict(self.snapshot), STATUS_KEY)
        except ImportError:
            self.storage_enabled = False
        except Exception as error:
            logging.warning("Could not publish Pyto proxy status: %s", error)
            self.storage_enabled = False

    def _notify(self, message):
        if not self.notifications_enabled:
            return
        try:
            import notifications
            notifications.send_notification(notifications.Notification(message=message))
        except ImportError:
            self.notifications_enabled = False
        except Exception as error:
            logging.warning("Could not send Pyto notification: %s", error)

    def starting(self):
        self._publish()

    def live_status(self, stats=None):
        """Pull current counters rather than the last persisted heartbeat."""
        data = dict(self.snapshot)
        if stats is not None and not self.finished:
            data.update(stats.snapshot())
        data["updated_at"] = time.time()
        return data

    def running(self):
        if self.finished:
            return
        self.snapshot.update(state="running", reason="Proxy listening", started_at=time.time())
        self._publish()
        self._notify(
            "Proxy started\nSOCKS {host}:{socks_port} | HTTP {host}:{http_port}".format(
                **self.snapshot
            )
        )

    def update(self, stats):
        if self.finished or time.monotonic() - self.last_publish < HEARTBEAT_INTERVAL:
            return
        self.snapshot.update(stats.snapshot())
        self._publish(refresh_widget=False)

    def finish(self, reason="Stopped by user", failed=False, stats=None, restarting=False):
        # Several cleanup paths run for a single shutdown; notify only once.
        if self.finished:
            return
        self.finished = True
        if stats is not None:
            self.snapshot.update(stats.snapshot())
        self.snapshot.update(state="failed" if failed else "stopped", reason=reason,
                             connections=0, in_mbps=0, out_mbps=0)
        self._publish(final=not restarting)
        if not restarting:
            self.widget_publisher.wait_closed()
        self._notify(f"Proxy {'failed' if failed else 'stopped'}\n{reason}")
