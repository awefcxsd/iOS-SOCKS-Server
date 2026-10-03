"""Shutdown helpers for hosts that interrupt the script's Python thread."""

import asyncio
import logging
import threading


SHUTDOWN_TIMEOUT = 5


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
        worker = threading.Thread(target=server.shutdown, daemon=True)
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
