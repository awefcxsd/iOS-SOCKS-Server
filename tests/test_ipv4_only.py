import asyncio
import socket
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from dns.asyncresolver import Resolver
from proxy_lib.proxy_server import AsyncProxyServer, Socks5AddressType, close_writer
from proxy_lib.socks5_server import AsyncSocks5Handler, UdpForwarder


class IPv4OnlyTests(unittest.IsolatedAsyncioTestCase):
    def make_server(self, **kwargs):
        resolver = Resolver(configure=False)
        resolver.nameservers = ["1.1.1.1", "2606:4700:4700::1111"]
        return AsyncProxyServer(AsyncSocks5Handler, resolver=resolver, **kwargs)

    async def test_unbound_dns_and_tcp_use_ipv4(self):
        server = self.make_server()
        self.assertEqual(server.resolver.nameservers, ["1.1.1.1"])
        server.resolver.resolve = AsyncMock(return_value=[SimpleNamespace(address="1.2.3.4")])
        with patch("asyncio.open_connection", new_callable=AsyncMock) as connect:
            await server.tcp_connect(Socks5AddressType.DOMAIN, ("example.com", 443))
        server.resolver.resolve.assert_awaited_once_with("example.com", "A", source=None)
        self.assertEqual(connect.call_args.args, ("1.2.3.4", 443))
        self.assertEqual(connect.call_args.kwargs["family"], socket.AF_INET)
        self.assertIsNone(connect.call_args.kwargs["local_addr"])

    async def test_ipv6_literals_do_not_connect_or_query_dns(self):
        server = self.make_server()
        server.resolver.resolve = AsyncMock()
        with patch("asyncio.open_connection", new_callable=AsyncMock) as connect:
            for address_type in (Socks5AddressType.IPV6, Socks5AddressType.DOMAIN):
                with self.assertRaisesRegex(OSError, "use IPv4"):
                    await server.tcp_connect(address_type, ("2001:4860::1", 443))
        connect.assert_not_awaited()
        server.resolver.resolve.assert_not_awaited()

    async def test_ipv6_only_dns_configuration_fails(self):
        resolver = Resolver(configure=False)
        resolver.nameservers = ["2606:4700:4700::1111"]
        with self.assertRaisesRegex(OSError, "IPv4 nameservers"):
            AsyncProxyServer(AsyncSocks5Handler, resolver=resolver)

    async def test_default_listener_and_unbound_udp_are_ipv4(self):
        server = self.make_server(listen_port=0)
        await server.start()
        forwarder = UdpForwarder("test", server, "127.0.0.1")
        try:
            self.assertTrue(server.server.sockets)
            self.assertTrue(all(s.family == socket.AF_INET for s in server.server.sockets))
            await forwarder.start()
            self.assertEqual(len(server._datagram_transports), 2)
            self.assertTrue(all(t.get_extra_info("socket").family == socket.AF_INET
                                for t in server._datagram_transports))
        finally:
            forwarder.close()
            await server.close()

    async def test_socks_ipv6_request_returns_address_type_not_supported(self):
        server = self.make_server(listen_hosts="127.0.0.1", listen_port=0)
        await server.start()
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", server.server.sockets[0].getsockname()[1])
        try:
            writer.write(b"\x05\x01\x00")
            await writer.drain()
            self.assertEqual(await asyncio.wait_for(reader.readexactly(2), 2), b"\x05\x00")
            writer.write(b"\x05\x01\x00\x04" + socket.inet_pton(socket.AF_INET6, "::1") + b"\x01\xbb")
            await writer.drain()
            self.assertEqual((await asyncio.wait_for(reader.readexactly(10), 2))[:2], b"\x05\x08")
        finally:
            await close_writer(writer)
            await server.close()
