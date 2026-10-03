import ast
import asyncio
import socket
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from dns.asyncresolver import Resolver
from proxy_lib.http_proxy_server import AsyncHTTPProxyHandler
from proxy_lib.proxy_server import AsyncProxyServer, Socks5AddressType, close_writer


class ProxyRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_connection_uses_changed_source(self):
        resolver = Resolver(configure=False)
        resolver.nameservers = ["1.1.1.1", "2606:4700:4700::1111"]
        server = AsyncProxyServer(
            AsyncHTTPProxyHandler, resolver=resolver,
            connect_host_ipv4="10.0.0.1",
            source_address_provider=lambda: ("10.0.0.2", None),
        )
        with patch("asyncio.open_connection", new_callable=AsyncMock) as connect:
            await server.tcp_connect(Socks5AddressType.IPV4, ("1.1.1.1", 443))
        self.assertEqual(connect.call_args.kwargs["local_addr"], ("10.0.0.2", 0))
        self.assertEqual(server.resolver_source, "10.0.0.2")
        self.assertEqual(resolver.nameservers, ["1.1.1.1", "2606:4700:4700::1111"])

    async def test_dns_family_can_change_after_network_change(self):
        resolver = Resolver(configure=False)
        resolver.nameservers = ["1.1.1.1", "2606:4700:4700::1111"]
        server = AsyncProxyServer(
            AsyncHTTPProxyHandler, resolver=resolver, connect_host_ipv4="10.0.0.1",
            source_address_provider=lambda: (None, "2001:db8::2"),
        )
        server.refresh_source_addresses()
        self.assertEqual(server.resolver.nameservers, ["2606:4700:4700::1111"])
        self.assertEqual(server.resolver_source, "2001:db8::2")
        server.resolver.resolve = AsyncMock(side_effect=OSError("DNS blocked"))
        with self.assertRaisesRegex(OSError, "DNS lookup failed.*DNS blocked"):
            await server.resolve_address(Socks5AddressType.DOMAIN, ("example.com", 443))
        self.assertEqual(server.resolver.resolve.call_args.kwargs["source"], "2001:db8::2")

    async def test_http_error_closes_client_socket(self):
        server = AsyncProxyServer(
            AsyncHTTPProxyHandler, listen_hosts="127.0.0.1", listen_port=0
        )
        await server.start()
        port = server.server.sockets[0].getsockname()[1]
        server.tcp_connect = AsyncMock(side_effect=OSError("route unavailable"))
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                writer.write(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com\r\n\r\n")
                await writer.drain()
                response = await asyncio.wait_for(reader.read(), timeout=2)
                self.assertIn(b"502", response)
                self.assertEqual(server.traffic_stats.connections, 0)
            finally:
                await close_writer(writer)
        finally:
            await server.close()

    async def test_unusable_dns_family_change_remains_an_error(self):
        resolver = Resolver(configure=False)
        resolver.nameservers = ["1.1.1.1"]
        server = AsyncProxyServer(
            AsyncHTTPProxyHandler, resolver=resolver, connect_host_ipv4="10.0.0.1",
            source_address_provider=lambda: (None, "2001:4860::2"),
        )
        for _ in range(2):
            with self.assertRaisesRegex(Exception, "suitable nameservers"):
                server.refresh_source_addresses()
        self.assertEqual(server.resolver_source, "10.0.0.1")
        self.assertEqual(server.resolver.nameservers, ["1.1.1.1"])

    async def test_http_early_eof_closes_socket(self):
        server = AsyncProxyServer(
            AsyncHTTPProxyHandler, listen_hosts="127.0.0.1", listen_port=0
        )
        await server.start()
        try:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", server.server.sockets[0].getsockname()[1]
            )
            try:
                writer.write_eof()
                self.assertEqual(await asyncio.wait_for(reader.read(), 2), b"")
            finally:
                await close_writer(writer)
        finally:
            await server.close()


class SourceSelectionTests(unittest.TestCase):
    def setUp(self):
        # Load only pure selection functions; importing the entrypoint probes iOS.
        source = Path(__file__).resolve().parents[1] / "socks5.py"
        tree = ast.parse(source.read_text())
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name in ("is_globally_routable", "current_source_addresses")]
        import ipaddress
        self.namespace = dict(socket=socket, ipaddress=ipaddress,
                              IFF_UP=1, IFF_RUNNING=0x40,
                              connect_interface_ipv4="pdp_ip0", connect_interface_ipv6="pdp_ip0",
                              CONNECT_HOST_IPV4="10.0.0.1", CONNECT_HOST_IPV6="2001:4860::1")
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), self.namespace)

    def select(self, interfaces):
        module = types.ModuleType("proxy_lib.ifaddrs")
        module.get_interfaces = lambda: interfaces
        with patch.dict(sys.modules, {"proxy_lib.ifaddrs": module}):
            return self.namespace["current_source_addresses"]()

    def interface(self, family, address):
        return types.SimpleNamespace(name="pdp_ip0", flags=0x41,
                                     addr=types.SimpleNamespace(family=family, address=address))

    def test_ipv4_keeps_working_when_ipv6_disappears(self):
        self.assertEqual(self.select([self.interface(socket.AF_INET, "10.0.0.2")]),
                         ("10.0.0.2", None))

    def test_missing_interfaces_do_not_fall_back_to_unbound_route(self):
        with self.assertRaisesRegex(OSError, "no active addresses"):
            self.select([])

    def test_link_local_ipv6_is_not_selected(self):
        self.assertEqual(self.select([
            self.interface(socket.AF_INET, "10.0.0.2"),
            self.interface(socket.AF_INET6, "fe80::1"),
        ]), ("10.0.0.2", None))


if __name__ == "__main__":
    unittest.main()
