"""Loopback-only lifecycle commands, independent of Pyto's process lifecycle."""

import logging
import json
import secrets
import socket
import threading

from proxy_lib.lifecycle import service_thread


CONTROL_KEY = "ios_socks_server.control.v1"


class PytoProxyControl:
    def __init__(self, on_stop, on_restart=None, get_status=None):
        self.on_stop = on_stop
        self.on_restart = on_restart
        self.command_requested = False
        self.get_status = get_status
        self.done = threading.Event()
        self.listener = None
        self.thread = None
        self.storage = None
        self.token = secrets.token_hex(24)

    def start(self):
        try:
            import userkeys
        except ImportError:
            return False
        self.storage = userkeys
        try:
            self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.listener.bind(("127.0.0.1", 0))
            self.listener.listen(2)
            self.listener.settimeout(0.2)
            self.storage.set({"port": self.listener.getsockname()[1],
                              "token": self.token,
                              "restart_supported": self.on_restart is not None,
                              "status_supported": self.get_status is not None}, CONTROL_KEY)
            self.thread = service_thread(target=self._serve, name="proxy-stop-control", daemon=True)
            self.thread.start()
            return True
        except Exception as error:
            logging.error("stop.py control unavailable: %s", error)
            self.stop()
            return False

    def _serve(self):
        while not self.done.is_set():
            try:
                client, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with client:
                client.settimeout(1)
                try:
                    with client.makefile("rb") as reader:
                        request = reader.readline(256)
                    token_line = self.token.encode("ascii") + b"\n"
                    if (self.get_status is not None
                            and secrets.compare_digest(request, b"STATUS " + token_line)):
                        client.sendall(json.dumps(self.get_status()).encode("utf-8") + b"\n")
                        continue
                    restart = secrets.compare_digest(request, b"RESTART " + token_line)
                    stop = secrets.compare_digest(request, token_line)
                    if not (stop or (restart and self.on_restart is not None)):
                        client.sendall(b"DENIED\n")
                        continue
                    if self.command_requested:
                        client.sendall(b"BUSY\n")
                        continue
                    self.command_requested = True
                    if restart:
                        self.on_restart()
                        client.sendall(b"RESTART REQUESTED\n")
                    else:
                        self.on_stop()
                        client.sendall(b"STOP REQUESTED\n")
                except (OSError, ValueError) as error:
                    logging.debug("Proxy stop control request failed: %s", error)

    def stop(self):
        self.done.set()
        if self.listener is not None:
            self.listener.close()
        if self.thread is not None and self.thread.ident is not None:
            self.thread.join(timeout=1.5)
        if self.storage is not None:
            try:
                if self.storage.get(CONTROL_KEY).get("token") == self.token:
                    self.storage.delete(CONTROL_KEY)
            except KeyError:
                pass
            except Exception as error:
                logging.warning("Could not clear proxy stop control: %s", error)
