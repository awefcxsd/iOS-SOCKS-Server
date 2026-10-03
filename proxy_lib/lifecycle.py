"""Shutdown helpers for hosts that interrupt the script's Python thread."""

import asyncio
import logging
import threading


SHUTDOWN_TIMEOUT = 5


def service_thread(**kwargs):
    """Avoid Pyto's Thread subclass registering helpers as the script owner."""
    thread_class = next(
        cls for cls in threading.Thread.__mro__
        if cls.__module__ == "threading" and cls.__name__ == "Thread"
    )
    return thread_class(**kwargs)


class PytoStopWatcher:
    def __init__(self, on_stop, interval=0.25, is_running=None):
        self.on_stop = on_stop
        self.interval = interval
        self.is_running = is_running
        self.done = threading.Event()
        self.thread = None

    def start(self):
        if self.is_running is None:
            path = getattr(threading.current_thread(), "script_path", None)
            if path is None:
                return False
            try:
                from pyto import Python
                self.is_running = lambda: bool(Python.shared.isScriptRunning(path))
                if not self.is_running():
                    return False
            except (ImportError, AttributeError):
                return False
        self.thread = service_thread(target=self._watch, name="pyto-stop-watcher", daemon=True)
        self.thread.start()
        return True

    def _watch(self):
        while not self.done.wait(self.interval):
            try:
                running = self.is_running()
            except Exception as error:
                logging.error("Pyto Stop watcher failed: %s", error)
                return
            if not running:
                self.on_stop()
                return

    def stop(self):
        self.done.set()


def cleanup_steps(*steps):
    """Try every cleanup action, including after Pyto's TaskExit/SystemExit."""
    for step in steps:
        try:
            step()
        except BaseException as error:
            logging.error("Shutdown action failed: %s: %s", type(error).__name__, error)


def stop_wpad_server(server, thread):
    # shutdown() waits for serve_forever(), which may already have been stopped
    # by Pyto. Never block the script thread on that wait.
    if thread is not None and thread.is_alive():
        worker = service_thread(target=server.shutdown, daemon=True)
        worker.start()
        worker.join(timeout=1)
    server.server_close()
    if thread is not None:
        thread.join(timeout=1)


def run_until_stopped(coroutine, stop_services):
    """Close sockets/audio before waiting for asyncio task cancellation."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def wake_for_interrupt():
        # Pyto injects an exception into the script thread. Keep selector waits
        # short so it can be delivered even while startup/network I/O is idle.
        loop.call_later(0.25, wake_for_interrupt)

    loop.call_soon(wake_for_interrupt)
    try:
        return loop.run_until_complete(coroutine)
    finally:
        try:
            stop_services()
        finally:
            try:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    done, unfinished = loop.run_until_complete(
                        asyncio.wait(pending, timeout=SHUTDOWN_TIMEOUT)
                    )
                    for task in done:
                        if not task.cancelled():
                            task.exception()
                    if unfinished:
                        logging.error("Shutdown timed out with %d tasks pending", len(unfinished))
            finally:
                asyncio.set_event_loop(None)
                loop.close()
