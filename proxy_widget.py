#!python3
"""Run once in Pyto, then select this script in a Pyto Run Script widget."""

from datetime import datetime, timedelta
import time

# Keep this script self-contained: Pyto's widget extension may copy/run only
# this file, without the project's sibling packages on its import path.
# This storage key and expiry must match proxy_lib/pyto_status.py.
STATUS_KEY = "ios_socks_server.status.v1"
STALE_AFTER = 60


def read_status():
    try:
        import userkeys
        value = userkeys.get(STATUS_KEY)
        return value if isinstance(value, dict) else {}
    except (ImportError, KeyError):
        return {}
    except Exception as error:
        print("Could not read Pyto proxy status:", error)
        return {}


def display_state(snapshot, now=None):
    state = snapshot.get("state", "unknown")
    if state in ("starting", "running"):
        try:
            age = (time.time() if now is None else now) - float(snapshot["updated_at"])
            if age < -1 or age >= STALE_AFTER:
                return "stale"
        except (KeyError, TypeError, ValueError):
            return "unknown"
    return state


def build_widget(wd, snapshot, date):
    state = display_state(snapshot, date.timestamp())
    labels = {
        "running": "Running (snapshot)", "starting": "Starting",
        "stopped": "Stopped", "failed": "Failed",
        "stale": "Status unconfirmed", "unknown": "No status yet",
    }
    color = (wd.COLOR_SYSTEM_GREEN if state == "running" else
             wd.COLOR_SYSTEM_RED if state == "failed" else wd.COLOR_SYSTEM_ORANGE)
    widget = wd.Widget()

    def text(value, size=12, tint=None):
        return wd.Text(str(value), font=wd.Font.system_font_of_size(size),
                       color=tint or wd.COLOR_LABEL)

    for size, layout in (("small", widget.small_layout),
                         ("medium", widget.medium_layout),
                         ("large", widget.large_layout)):
        layout.set_background_color(wd.COLOR_SYSTEM_BACKGROUND)
        layout.set_link("status")
        layout.add_row([text("iOS Proxy", 16)])
        layout.add_row([text(labels.get(state, "No status yet"), 12, color)])
        layout.add_vertical_spacer()
        if snapshot:
            layout.add_row([text(snapshot.get("host", ""))])
            if size != "small":
                layout.add_row([text("SOCKS {} | HTTP {}".format(
                    snapshot.get("socks_port", ""), snapshot.get("http_port", "")))])
                layout.add_row([text("In {:.2f} | Out {:.2f} Mbps".format(
                    snapshot.get("in_mbps", 0), snapshot.get("out_mbps", 0)))])
            layout.add_row([text("{} connections".format(snapshot.get("connections", 0)))])
            if size == "large":
                total = snapshot.get("in_bytes", 0) + snapshot.get("out_bytes", 0)
                layout.add_row([text("Transferred {:.2f} MB".format(total / (1024 * 1024)))])
                layout.add_row([text("{} errors".format(snapshot.get("errors", 0)))])
                layout.add_row([text(snapshot.get("reason", ""))])
            try:
                updated = datetime.fromtimestamp(snapshot["updated_at"])
                layout.add_row([text("Updated " + updated.strftime("%H:%M:%S"), 10)])
            except (KeyError, TypeError, ValueError, OSError):
                pass
        else:
            layout.add_row([text("Run socks5.py in Pyto")])
    return widget


def main():
    try:
        import widgets as wd
    except ImportError:
        print("This widget requires Pyto on iOS. Run proxy_widget.py in Pyto.")
        return
    snapshot = read_status()

    class ProxyProvider(wd.TimelineProvider):
        def timeline(self):
            now = datetime.now()
            dates = [now]
            # Pre-render an unconfirmed state, so an old running snapshot does
            # not remain green indefinitely when iOS delays the next reload.
            if display_state(snapshot, now.timestamp()) in ("running", "starting"):
                dates.append(datetime.fromtimestamp(float(snapshot["updated_at"]) + STALE_AFTER + 1))
            return dates

        def reload_time(self):
            # Pyto counts this delay from the LAST timeline entry. Request a
            # reload when the running snapshot expires, or in a minute when
            # only a stopped/unconfirmed entry is available.
            return timedelta(seconds=0 if len(self.timeline()) > 1 else 60)

        def widget(self, date):
            return build_widget(wd, snapshot, date)

    if wd.link is not None:
        print("Proxy status:", display_state(snapshot))
        print("PAC URL:", snapshot.get("pac_url", "Run socks5.py first"))
        print("Last event:", snapshot.get("reason", "No status yet"))
    wd.provide_timeline(ProxyProvider())


# Pyto's app uses __main__, its Home Screen extension uses runpy.run_path's
# default <run_path>, and its fallback widget-tap URL uses widget.
if __name__ in ("__main__", "<run_path>", "widget"):
    main()
