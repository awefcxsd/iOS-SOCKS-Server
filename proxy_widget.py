#!python3
"""Pull live proxy data and save a Pyto In App widget named iOS Proxy."""

from datetime import datetime
import json
import socket
import time

# This standalone launcher uses only Pyto APIs and the proxy's loopback status
# endpoint. It never imports the proxy package or reads a saved traffic record.
CONTROL_KEY = "ios_socks_server.control.v1"
WIDGET_KEY = "iOS Proxy"
STALE_AFTER = 60


def read_live_status():
    try:
        import userkeys
        control = userkeys.get(CONTROL_KEY)
    except (ImportError, KeyError):
        return {"state": "unavailable", "reason": "Start socks5.py to publish live status"}
    except Exception as error:
        return {"state": "unavailable", "reason": "Could not read proxy control: " + str(error)}
    if (not isinstance(control, dict)
            or type(control.get("port")) is not int
            or not 0 < control["port"] <= 65535
            or not isinstance(control.get("token"), str)
            or len(control["token"]) != 48
            or any(character not in "0123456789abcdef" for character in control["token"])
            or control.get("status_supported") is not True):
        return {"state": "unavailable", "reason": "Run the updated socks5.py first"}
    try:
        with socket.create_connection(("127.0.0.1", control["port"]), timeout=2) as client:
            client.sendall(b"STATUS " + control["token"].encode("ascii") + b"\n")
            with client.makefile("rb") as reader:
                response = reader.readline(16384)
        data = json.loads(response)
        if not isinstance(data, dict) or data.get("state") not in ("starting", "running", "stopped", "failed"):
            raise ValueError("Invalid server status")
        return data
    except (OSError, ValueError) as error:
        return {"state": "unavailable", "reason": "Live proxy status unavailable: " + str(error)}


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
        "running": "Running", "starting": "Starting",
        "stopped": "Stopped", "failed": "Failed",
        "stale": "Status unconfirmed", "unknown": "No status yet",
        "unavailable": "Server unavailable",
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
        if snapshot.get("host"):
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
                layout.add_row([text("Updated", 10), wd.DynamicDate(
                    updated, style=wd.DATE_STYLE_RELATIVE,
                    font=wd.Font.system_font_of_size(10), color=wd.COLOR_SECONDARY_LABEL)])
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
    snapshot = read_live_status()

    if wd.link is not None:
        print("Proxy status:", display_state(snapshot))
        print("PAC URL:", snapshot.get("pac_url", "Run socks5.py first"))
        print("Last event:", snapshot.get("reason", "No status yet"))
    wd.save_widget(build_widget(wd, snapshot, datetime.now()), WIDGET_KEY)
    print("Select Pyto > In App > iOS Proxy for the Home Screen widget.")


# Pyto's app uses __main__, its Home Screen extension uses runpy.run_path's
# default <run_path>, and its fallback widget-tap URL uses widget.
if __name__ in ("__main__", "<run_path>", "widget"):
    main()
