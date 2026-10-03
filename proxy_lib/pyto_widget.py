"""Publish current server counters to a named Pyto In App widget."""

import logging
from pathlib import Path
from queue import Empty, Full, Queue
from datetime import datetime

from proxy_lib.lifecycle import service_thread


class InAppWidgetPublisher:
    def __init__(self, enabled=True):
        self.enabled = enabled
        self.jobs = Queue(maxsize=1)
        self.thread = None
        self.finished = False

    def submit(self, data, final=False):
        if not self.enabled or self.finished:
            return
        if final:
            self.finished = True
        # Coalesce updates if iOS takes longer to save than the publish interval.
        try:
            self.jobs.get_nowait()
        except Empty:
            pass
        try:
            self.jobs.put_nowait((dict(data), final))
        except Full:
            return
        if self.thread is None:
            self.thread = service_thread(target=self._publish, name="proxy-in-app-widget", daemon=True)
            # Use the widget script for the tap bookmark, keeping a tap from
            # opening/running another socks5.py server.
            self.thread.script_path = str(Path(__file__).resolve().parents[1] / "proxy_widget.py")
            self.thread.start()

    def _publish(self):
        try:
            import widgets as wd
            from proxy_widget import WIDGET_KEY, build_widget
            while True:
                data, final = self.jobs.get()
                widget = build_widget(wd, data, datetime.now())
                # Pyto's public save_widget also presents a preview. Use its
                # native save operation directly for automatic server updates,
                # so every heartbeat does not open another preview sheet.
                native_type = wd.__PyWidget__
                native_widget = native_type.alloc().init()
                native_widget.scriptPath = self.thread.script_path
                for family, layout in enumerate((widget.small_layout, widget.medium_layout,
                                                  widget.large_layout)):
                    native_widget.addView(layout.__widget_view__, family=family)
                native_type.addWidget(native_widget, key=WIDGET_KEY)
                if final:
                    return
        except ImportError:
            self.enabled = False
        except Exception as error:
            self.enabled = False
            logging.error("Could not update Pyto In App widget: %s", error)

    def wait_closed(self):
        """Give the final stopped entry a chance to save before a restart."""
        if self.thread is not None:
            self.thread.join(timeout=2)
