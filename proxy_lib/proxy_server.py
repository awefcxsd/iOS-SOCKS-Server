""" General base class for proxies """

import asyncio
import copy
import logging
import random
import socket
from dataclasses import dataclass
from enum import IntEnum
from typing import Callable, Sequence, Type

from dns.asyncresolver import Resolver
from dns.inet import af_for_address

from . import status

SocketAddress = tuple[str, int]
Connection = tuple[asyncio.StreamReader, asyncio.StreamWriter]


@dataclass
class GenericAddress:
    ipv4: SocketAddress | None = None
    ipv6: SocketAddress | None = None


CONNECT_TIMEOUT = 75  # seconds
CLOSE_TIMEOUT = 5  # seconds


async def close_writer(writer: asyncio.StreamWriter) -> None:
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), timeout=CLOSE_TIMEOUT)
    except asyncio.CancelledError:
        writer.transport.abort()
        raise
    except (OSError, asyncio.TimeoutError):
        writer.transport.abort()


def normalize_socket_address(address: Sequence | None) -> SocketAddress | None:
    """Return the host/port pair from a platform socket address tuple."""
    if address is None:
        return None
    if len(address) < 2:
        raise ValueError("socket address is missing host or port: %r" % (address,))
    return address[0], address[1]


async def forwarder_loop(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    stat_fn: Callable[[int], None],
) -> None:
    try:
        while 1:
            buf = await reader.read(65536)
            if not buf:
                break
            stat_fn(len(buf))
            writer.write(buf)
            await writer.drain()
    finally:
        await close_writer(writer)


# XXX: should make this a more generic address type enum and convert from socks5
class Socks5AddressType(IntEnum):
    IPV4 = 1
    DOMAIN = 3
    IPV6 = 4


class AsyncProxyHandler:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        server: "AsyncProxyServer",
    ):
        self.reader = reader
        self.writer = writer
        self.server = server

        peer_addr = writer.get_extra_info("peername")
        if peer_addr is None:
            self.log_tag = "<unknown>"
        elif len(peer_addr) == 2:
            # IPv4
            self.log_tag = "%s:%s" % peer_addr
        elif len(peer_addr) == 4:
            # IPv6
            self.log_tag = "[%s]:%s" % peer_addr[:2]
        else:
            self.log_tag = "[%s]" % (peer_addr,)

    async def tcp_forward(self, connection: Connection) -> None:
        s_reader, s_writer = connection
        await asyncio.gather(
            forwarder_loop(
                s_reader, self.writer, self.server.traffic_stats.add_inbound
            ),
            forwarder_loop(
                self.reader, s_writer, self.server.traffic_stats.add_outbound
            ),
            return_exceptions=True,
        )

    async def handle(self) -> None:
        pass


class AsyncProxyServer:
    def __init__(
        self,
        handler_class: Type[AsyncProxyHandler],
        listen_hosts: str | Sequence[str] = "0.0.0.0",
        listen_port: int = 9876,
        traffic_stats: status.TrafficStats | None = None,
        resolver: Resolver | None = None,
        connect_host_ipv4: str | None = None,
        source_address_provider: Callable[[], tuple[str | None, str | None]] | None = None,
    ):
        self.handler_class = handler_class
        self.listen_hosts = listen_hosts
        self.listen_port = listen_port
        self.traffic_stats = traffic_stats or status.SimpleTrafficStats()
        # Each listener needs its own nameserver list when source families change.
        self.resolver = copy.copy(resolver) if resolver is not None else Resolver()
        self._nameservers = list(self.resolver.nameservers)
        self.source_address_provider = source_address_provider
        self.connect_host_ipv4 = connect_host_ipv4
        self.server: asyncio.Server | None = None
        self._closing = False
        self._client_tasks = set()
        self._writers = set()
        self._outbound_writers = {}
        self._datagram_transports = set()
        self._configure_resolver_source()

    def _configure_resolver_source(self) -> None:
        self.resolver_source = self.connect_host_ipv4
        self.resolver.nameservers = [
            ns for ns in self._nameservers if af_for_address(ns) == socket.AF_INET
        ]
        if not self.resolver.nameservers:
            raise OSError("Resolver does not have any suitable IPv4 nameservers!")

    def refresh_source_addresses(self) -> None:
        if self.source_address_provider is None:
            return
        ipv4, _ = self.source_address_provider()
        if ipv4 is None:
            raise OSError("Selected outbound interface has no active IPv4 address")
        if ipv4 == self.connect_host_ipv4:
            return
        previous = self.connect_host_ipv4
        self.connect_host_ipv4 = ipv4
        try:
            self._configure_resolver_source()
        except Exception:
            self.connect_host_ipv4 = previous
            self._configure_resolver_source()
            raise
        logging.warning("Proxy source address changed: IPv4 %s -> %s", previous, ipv4)

    async def start(self) -> None:
        self._closing = False
        self.server = await asyncio.start_server(
            self.client_connected,
            host=self.listen_hosts,
            port=self.listen_port,
            reuse_address=True,
            family=socket.AF_INET,
        )
        if self._closing:
            self.server.close()

    async def run(self) -> None:
        await self.start()
        try:
            await self.server.serve_forever()
        finally:
            await self.close()

    def stop_now(self) -> None:
        """Release sockets without waiting for coroutine cancellation."""
        self._closing = True
        if self.server is not None:
            self.server.close()
        for writer in tuple(self._writers):
            writer.transport.abort()
        for transport in tuple(self._datagram_transports):
            transport.close()
        for task in tuple(self._client_tasks):
            task.cancel()

    async def close(self) -> None:
        self.stop_now()
        if self._client_tasks:
            await asyncio.wait(tuple(self._client_tasks), timeout=CLOSE_TIMEOUT)
        if self.server is not None:
            await asyncio.wait_for(self.server.wait_closed(), timeout=CLOSE_TIMEOUT)
            self.server = None

    async def client_connected(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if self._closing:
            writer.transport.abort()
            return
        task = asyncio.current_task()
        self._client_tasks.add(task)
        self._writers.add(writer)
        self._outbound_writers[task] = set()
        self.traffic_stats.add_connection()
        try:
            handler = self.handler_class(reader, writer, server=self)
            await handler.handle()
        finally:
            self.traffic_stats.remove_connection()
            try:
                writers = self._outbound_writers.pop(task, set()) | {writer}
                await asyncio.gather(*(close_writer(item) for item in writers))
            finally:
                self._writers.difference_update(writers)
                self._client_tasks.discard(task)

    async def ipv4_connect(self, address: SocketAddress) -> Connection:
        address = normalize_socket_address(address)
        local_addr = (
            (self.connect_host_ipv4, 0) if self.connect_host_ipv4 is not None else None
        )
        return await asyncio.wait_for(
            asyncio.open_connection(
                address[0],
                address[1],
                family=socket.AF_INET,
                local_addr=local_addr,
            ),
            timeout=CONNECT_TIMEOUT,
        )

    async def tcp_connect(
        self, address_type: int, address: SocketAddress
    ) -> Connection:
        resolved = await self.resolve_address(address_type, address)
        connection = await self.ipv4_connect(resolved.ipv4)
        writer = connection[1]
        if self._closing:
            writer.transport.abort()
            raise asyncio.CancelledError()
        task = asyncio.current_task()
        if task in self._outbound_writers:
            self._writers.add(writer)
            self._outbound_writers[task].add(writer)
        return connection

    async def _resolve_domain(self, address: SocketAddress) -> GenericAddress:
        domain, port = address
        try:
            family = af_for_address(domain)
        except ValueError:
            family = None
        if family == socket.AF_INET:
            return GenericAddress(ipv4=address)
        if family == socket.AF_INET6:
            raise OSError("IPv6 destinations are not supported; use IPv4")

        try:
            answers = await self.resolver.resolve(domain, "A", source=self.resolver_source)
            if answers:
                return GenericAddress(ipv4=(random.choice(answers).address, port))
            raise OSError("No IPv4 addresses returned")
        except Exception as error:
            raise OSError(
                "DNS lookup failed for %s (source %s): A: %s"
                % (domain, self.resolver_source or "system default", error)
            ) from error

    async def resolve_address(
        self, address_type: int, address: SocketAddress
    ) -> GenericAddress:
        address = normalize_socket_address(address)
        if address_type not in (Socks5AddressType.IPV4, Socks5AddressType.DOMAIN):
            raise OSError("Address type is not supported; use IPv4")
        self.refresh_source_addresses()
        if address_type == Socks5AddressType.IPV4:
            socket.inet_pton(socket.AF_INET, address[0])
            return GenericAddress(ipv4=address)
        return await self._resolve_domain(address)
