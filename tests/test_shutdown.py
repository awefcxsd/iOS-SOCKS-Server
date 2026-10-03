import ast
import asyncio
import socket
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from proxy_lib.background_audio import BackgroundAudio
from proxy_lib.http_proxy_server import AsyncHTTPProxyHandler
from proxy_lib.lifecycle import (
    PytoStopWatcher, cleanup_steps, run_until_stopped, service_thread, stop_wpad_server,
)
from proxy_lib.proxy_server import AsyncProxyServer, close_writer
from proxy_lib.socks5_server import AsyncSocks5Handler


def wpad_factory():
    path = Path(__file__).resolve().parents[1] / "socks5.py"
    tree = ast.parse(path.read_text())
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef)
             and node.name == "create_wpad_server"]
    namespace = {"service_thread": service_thread}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
    return namespace["create_wpad_server"]


class ShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_clients_close_and_listener_can_restart(self):
        for handler in (AsyncHTTPProxyHandler, AsyncSocks5Handler):
            server = AsyncProxyServer(handler, listen_hosts="127.0.0.1", listen_port=0)
            await server.start()
            port = server.server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            await asyncio.sleep(0)
            try:
                await asyncio.wait_for(server.close(), 2)
                self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")
                self.assertEqual(server.traffic_stats.connections, 0)
                self.assertFalse(server._client_tasks)
                server.listen_port = port
                await server.start()
            finally:
                await close_writer(writer)
                await server.close()

    async def test_open_http_tunnel_closes_upstream_and_client(self):
        upstream_closed = asyncio.Event()

        async def upstream_client(reader, writer):
            try:
                await reader.read()
            finally:
                await close_writer(writer)
                upstream_closed.set()

        upstream = await asyncio.start_server(upstream_client, "127.0.0.1", 0)
        upstream_port = upstream.sockets[0].getsockname()[1]
        server = AsyncProxyServer(AsyncHTTPProxyHandler, listen_hosts="127.0.0.1", listen_port=0)
        await server.start()
        reader, writer = await asyncio.open_connection("127.0.0.1", server.server.sockets[0].getsockname()[1])
        try:
            writer.write(f"CONNECT 127.0.0.1:{upstream_port} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
            await writer.drain()
            response = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
            self.assertIn(b"200", response)
            await asyncio.wait_for(server.close(), 2)
            self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")
            await asyncio.wait_for(upstream_closed.wait(), 1)
            self.assertFalse(server._writers)
        finally:
            await close_writer(writer)
            await server.close()
            upstream.close()
            await upstream.wait_closed()

    async def test_udp_association_closes_all_relays(self):
        server = AsyncProxyServer(AsyncSocks5Handler, listen_hosts="127.0.0.1", listen_port=0,
                                  connect_host_ipv4="127.0.0.1")
        await server.start()
        reader, writer = await asyncio.open_connection("127.0.0.1", server.server.sockets[0].getsockname()[1])
        try:
            writer.write(b"\x05\x01\x00")
            await writer.drain()
            self.assertEqual(await asyncio.wait_for(reader.readexactly(2), 1), b"\x05\x00")
            writer.write(b"\x05\x03\x00\x01\x00\x00\x00\x00\x00\x00")
            await writer.drain()
            reply = await asyncio.wait_for(reader.readexactly(10), 1)
            self.assertEqual(reply[:2], b"\x05\x00")
            transports = tuple(server._datagram_transports)
            self.assertEqual(len(transports), 2)
            await asyncio.wait_for(server.close(), 2)
            self.assertTrue(all(transport.is_closing() for transport in transports))
            self.assertFalse(server._datagram_transports)
        finally:
            await close_writer(writer)
            await server.close()


class ScriptStopTests(unittest.TestCase):
    def test_service_threads_do_not_use_pytos_registering_subclass(self):
        original = threading.Thread
        registered = Mock()

        class PytoThread(original):
            def run(self):
                registered()
                super().run()

        ran = threading.Event()
        with patch("threading.Thread", PytoThread):
            thread = service_thread(target=ran.set, daemon=True)
            thread.start()
            thread.join(timeout=1)
        self.assertTrue(ran.is_set())
        registered.assert_not_called()

    def test_native_stop_closes_sockets_without_running_event_loop(self):
        server = AsyncProxyServer(AsyncHTTPProxyHandler)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        client = socket.create_connection(("127.0.0.1", port), timeout=1)
        accepted, _ = listener.accept()
        server.server = Mock(sockets=[listener])
        server._writers.add(Mock(get_extra_info=Mock(return_value=accepted)))
        running = threading.Event()
        stopped = threading.Event()
        running.set()

        def stop():
            server.emergency_stop()
            stopped.set()

        watcher = PytoStopWatcher(stop, interval=0.01, is_running=running.is_set)
        try:
            watcher.start()
            # No event loop exists to process transport-close callbacks.
            running.clear()
            self.assertTrue(stopped.wait(timeout=1))
            self.assertEqual(client.recv(1), b"")
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", port))
        finally:
            watcher.stop()
            if watcher.thread is not None:
                watcher.thread.join(timeout=1)
            client.close()
            accepted.close()
            listener.close()

    def test_partial_startup_failure_releases_first_listener_and_audio(self):
        servers = []
        ports = []
        audio_stop = Mock()
        occupied = socket.socket()
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            occupied.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()

        async def main():
            first = AsyncProxyServer(AsyncHTTPProxyHandler, listen_hosts="127.0.0.1", listen_port=0)
            servers.append(first)
            await first.start()
            port = first.server.sockets[0].getsockname()[1]
            ports.append(port)
            second = AsyncProxyServer(AsyncHTTPProxyHandler, listen_hosts="127.0.0.1",
                                      listen_port=occupied.getsockname()[1])
            servers.append(second)
            await second.start()

        def stop():
            cleanup_steps(*(server.stop_now for server in servers), audio_stop)

        try:
            with self.assertRaises(OSError):
                run_until_stopped(main(), stop)
        finally:
            occupied.close()
        audio_stop.assert_called_once()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", ports[0]))

    def test_interrupt_releases_listener_before_task_cleanup(self):
        for exception in (SystemExit, KeyboardInterrupt):
            with self.subTest(exception=exception):
                stopped = threading.Event()
                servers = []
                ports = []
                cleaned = []

                def interrupt():
                    raise exception()

                def stop():
                    for server in servers:
                        server.stop_now()
                    stopped.set()

                async def main():
                    server = AsyncProxyServer(AsyncHTTPProxyHandler, listen_hosts="127.0.0.1", listen_port=0)
                    servers.append(server)
                    await server.start()
                    ports.append(server.server.sockets[0].getsockname()[1])
                    asyncio.get_running_loop().call_soon(interrupt)
                    try:
                        await asyncio.Event().wait()
                    finally:
                        self.assertTrue(stopped.is_set())
                        await asyncio.sleep(0.01)
                        cleaned.append(True)

                with self.assertRaises(exception):
                    run_until_stopped(main(), stop)
                self.assertEqual(cleaned, [True])
                with socket.socket() as probe:
                    probe.bind(("127.0.0.1", ports[0]))

    def test_cleanup_continues_after_task_exit(self):
        class TaskExit(BaseException):
            pass

        player = Mock()
        player.stop.side_effect = TaskExit()
        session = Mock()
        audio = BackgroundAudio()
        audio.player = player
        audio.audio_session = session
        audio.native_session_active = True
        with self.assertLogs(level="ERROR"):
            audio.stop()
        session.setActive_withOptions_error_.assert_called_once_with(False, 1, None)
        self.assertIsNone(audio.player)

    def test_wpad_idle_request_does_not_block_shutdown(self):
        server = wpad_factory()("127.0.0.1", 0, "127.0.0.1", 9876)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        with socket.create_connection(server.server_address, timeout=2) as client:
            try:
                stop_wpad_server(server, thread)
                self.assertFalse(thread.is_alive())
                self.assertEqual(client.recv(1), b"")
            finally:
                server.server_close()

    def test_wpad_thread_already_stopped(self):
        server = Mock()
        thread = Mock()
        thread.is_alive.return_value = False
        stop_wpad_server(server, thread)
        server.shutdown.assert_not_called()
        server.server_close.assert_called_once()

    def test_all_cleanup_steps_run_after_failure(self):
        failure = Mock(side_effect=RuntimeError("cleanup failed"))
        last = Mock()
        with self.assertLogs(level="ERROR"):
            cleanup_steps(failure, last)
        last.assert_called_once()


if __name__ == "__main__":
    unittest.main()
