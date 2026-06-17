#!python3
# Socks5/HTTP Proxy server for Pythonista by @nneonneo
# Pretty statistics view and IPv6 support added by @philrosenthal

import asyncio
import ipaddress
import logging
import socket
import threading

from proxy_lib.background_audio import BackgroundAudio
from proxy_lib.http_proxy_server import AsyncHTTPProxyHandler
from proxy_lib.proxy_server import AsyncProxyServer
from proxy_lib.socks5_server import AsyncSocks5Handler
from proxy_lib.status import StatusMonitor

logging.basicConfig(level=logging.ERROR)

# IP over which the proxy will be available (probably WiFi IP)
PROXY_HOST = "172.20.10.1"
# IP over which the proxy will attempt to connect to the Internet
CONNECT_HOST_IPV4 = None
CONNECT_HOST_IPV6 = None
# Time out connections after being idle for this long (in seconds)
IDLE_TIMEOUT = 1800

LISTEN_HOST = "0.0.0.0"
SOCKS_PORT = 9876
HTTP_PORT = 9877
WPAD_PORT = 8088

USE_PHONE_VPN = True
CUSTOM_RESOLVERS = []
# Loop silent audio while running so Pythonista can continue executing after
# iOS sends it to the background. iOS may still suspend or terminate the app.
KEEP_ALIVE_WITH_AUDIO = True
# Play a quiet 440 Hz tone instead of silence to verify background playback.
BACKGROUND_AUDIO_TEST_TONE = False

# Stop the server when the WiFi connection used at startup goes away. iOS does
# not reliably expose the SSID to Pyto, so this name is a label for the network
# that must be connected when the script starts.
EXIT_ON_WIFI_DISCONNECT = True
WIFI_NETWORK_NAME = "Subaru_5G"
WIFI_CHECK_INTERVAL = 2
WIFI_DISCONNECT_CHECKS = 3
IFF_UP = 0x1
IFF_RUNNING = 0x40
CONNECTIVITY_TEST_TIMEOUT = 5
IPV4_TEST_ADDRESS = ("1.1.1.1", 80)
IPV6_TEST_ADDRESS = ("2606:4700:4700::1111", 80)

# Try to keep the screen from turning off (iOS)
try:
    import console
    from objc_util import on_main_thread

    on_main_thread(console.set_idle_timer_disabled)(True)
except ImportError:
    pass


def is_globally_routable(ipv6_address):
    non_routable_networks = [
        "ff00::/8",  # Multicast address range
        "fe80::/10",  # Link-local address range
        "fc00::/7",  # Unique local address range
        "::/8",  # Unspecified address range
        "2001:db8::/32",  # Documentation address range
        "2001::/32",  # Teredo address range
        "2002::/16",  # 6to4 address range
        "ff02::/16",  # Link-local multicast address range
    ]
    for network in non_routable_networks:
        if ipaddress.ip_address(ipv6_address) in ipaddress.ip_network(network):
            return False
    return True


def test_tcp_connectivity(family, source_address, target_address):
    test_socket = socket.socket(family, socket.SOCK_STREAM)
    try:
        test_socket.settimeout(CONNECTIVITY_TEST_TIMEOUT)
        if source_address:
            test_socket.bind((source_address, 0))
        test_socket.connect(target_address)
        return None
    except Exception as e:
        return e
    finally:
        test_socket.close()


DEFAULT_RESOLVERS = [
    "1.0.0.1",
    "1.1.1.1",
    "8.8.8.8",
    "2606:4700:4700::1111",
    "2606:4700:4700::1001",
    "2001:4860:4860::8844",
]

try:
    # TODO: configurable DNS (or find a way to use the cell network's own DNS)
    import dns.asyncresolver

    resolver = dns.asyncresolver.Resolver(configure=False)
    resolver.nameservers += CUSTOM_RESOLVERS or DEFAULT_RESOLVERS
except ImportError:
    # pip install dnspython
    print("Warning: dnspython not available; falling back to system DNS")
    resolver = None

try:
    # We want the WiFi address so that clients know what IP to use.
    # We want the non-WiFi (cellular?) address so that we can force network
    #  traffic to go over that network. This allows the proxy to correctly
    #  forward traffic to the cell network even when the WiFi network is
    #  internet-enabled but limited (e.g. firewalled)

    from collections import defaultdict

    from proxy_lib import ifaddrs

    initial_output = ""
    ipv4_output = ""
    ipv6_output = ""
    wifi_interface_name = None
    wifi_interface_address = None

    interfaces = ifaddrs.get_interfaces()
    iftypes = defaultdict(list)

    for iface in interfaces:
        if not iface.addr:
            continue
        if iface.name.startswith("lo"):
            continue
        # XXX implement better classification of interfaces
        if iface.name.startswith("en"):
            iftypes["en"].append(iface)
        elif iface.name.startswith("bridge"):
            iftypes["bridge"].append(iface)
        elif iface.name.startswith("utun"):
            iftypes["vpn"].append(iface)
        else:
            iftypes["cell"].append(iface)

    if iftypes["vpn"] and USE_PHONE_VPN:
        ipv4_output += "VPN use enabled (change with USE_PHONE_VPN)\n"
        new_ifaces = []
        new_ifaces.extend(iftypes["vpn"])
        new_ifaces.extend(iftypes["cell"])
        iftypes["cell"] = new_ifaces

    if iftypes["bridge"]:
        iface = next(
            (
                iface
                for iface in iftypes["bridge"]
                if iface.addr.family == socket.AF_INET
            ),
            None,
        )
        if iface:
            wifi_interface_name = iface.name
            wifi_interface_address = iface.addr.address
            initial_output = (
                "Assuming proxy will be accessed over hotspot (%s) at %s\n"
                % (iface.name, iface.addr.address)
            )
            PROXY_HOST = iface.addr.address
    elif iftypes["en"]:
        iface = next(
            (iface for iface in iftypes["en"] if iface.addr.family == socket.AF_INET),
            None,
        )
        if iface:
            wifi_interface_name = iface.name
            wifi_interface_address = iface.addr.address
            initial_output += (
                "Assuming proxy will be accessed over WiFi (%s) at %s\n"
                % (iface.name, iface.addr.address)
            )
            PROXY_HOST = iface.addr.address
    else:
        initial_output += (
            "Warning: could not get WiFi address; assuming %s\n" % PROXY_HOST
        )

    if iftypes["cell"]:
        iface_ipv4 = next(
            (iface for iface in iftypes["cell"] if iface.addr.family == socket.AF_INET),
            None,
        )
        iface_ipv6 = None

        is_vpn = iface_ipv4 and iface_ipv4.name.startswith("utun")

        if iface_ipv4:
            iface_ipv4.addr.address
            ipv4_error = test_tcp_connectivity(
                socket.AF_INET,
                iface_ipv4.addr.address,
                IPV4_TEST_ADDRESS,
            )
            if ipv4_error is None:
                ipv4_output += (
                    "Will connect to IPv4 servers over interface %s at %s\n"
                    % (
                        iface_ipv4.name,
                        iface_ipv4.addr.address,
                    )
                )
                CONNECT_HOST_IPV4 = iface_ipv4.addr.address
            else:
                ipv4_output += (
                    "Failed to connect to %s:%d over IPv4 interface %s at %s due to: %s\n"
                    "Will connect to IPv4 servers using the system default route\n"
                    % (
                        IPV4_TEST_ADDRESS[0],
                        IPV4_TEST_ADDRESS[1],
                        iface_ipv4.name,
                        iface_ipv4.addr.address,
                        ipv4_error,
                    )
                )
                CONNECT_HOST_IPV4 = None

            # Create a list of all IPv6 addresse that are globally routable and match the IPv4 interface
            iface_ipv6_list = [
                iface
                for iface in iftypes["cell"]
                if iface.addr.family == socket.AF_INET6
                and iface.addr.address
                and (is_globally_routable(iface.addr.address) if not is_vpn else True)
                and iface.name == iface_ipv4.name
            ]

            # Select the last IPv6 address to select the temporary address for reduced tracking
            iface_ipv6 = iface_ipv6_list[-1] if iface_ipv6_list else None

        if iface_ipv6 is None and not is_vpn:
            # Create a list of all IPv6 addresses that are globally routable
            iface_ipv6_list = [
                iface
                for iface in iftypes["cell"]
                if iface.addr.family == socket.AF_INET6
                and iface.addr.address
                and is_globally_routable(iface.addr.address)
            ]

            # Select the last IPv6 address to select the temporary address for reduced tracking
            iface_ipv6 = iface_ipv6_list[-1] if iface_ipv6_list else None

        if iface_ipv6:
            iface_ipv6.addr.address
            ipv6_output += "Will connect to IPv6 servers over interface %s at %s\n" % (
                iface_ipv6.name,
                iface_ipv6.addr.address,
            )
            # Test IPv6 connectivity
            ipv6_error = test_tcp_connectivity(
                socket.AF_INET6,
                iface_ipv6.addr.address,
                IPV6_TEST_ADDRESS,
            )
            if ipv6_error is None:
                CONNECT_HOST_IPV6 = iface_ipv6.addr.address
            else:
                ipv6_output += (
                    "Failed to connect to %s:%d over IPv6 due to: %s\n"
                    % (IPV6_TEST_ADDRESS[0], IPV6_TEST_ADDRESS[1], ipv6_error)
                )
                CONNECT_HOST_IPV6 = None

    initial_output += ipv4_output + ipv6_output
    print(initial_output)
except Exception as e:
    logging.error("Address detection failed: %s: %s", (type(e).__name__, e))
    import traceback

    traceback.print_exc()

    interfaces = None
    wifi_interface_name = None
    wifi_interface_address = None


def wifi_connection_is_active(interface_name, interface_address):
    """Return whether the WiFi interface still has its startup IPv4 address."""
    try:
        from proxy_lib import ifaddrs

        return any(
            iface.name == interface_name
            and iface.addr
            and iface.addr.family == socket.AF_INET
            and iface.addr.address == interface_address
            and iface.flags & IFF_UP
            and iface.flags & IFF_RUNNING
            for iface in ifaddrs.get_interfaces()
        )
    except Exception as e:
        logging.warning("Could not check WiFi connection: %s", e)
        return True


def monitor_wifi_connection(
    interface_name,
    interface_address,
    stop_event,
    on_disconnect,
):
    print(
        "WiFi disconnect monitor started for {} at {}".format(
            interface_name, interface_address
        )
    )
    missed_checks = 0
    while not stop_event.wait(WIFI_CHECK_INTERVAL):
        if wifi_connection_is_active(interface_name, interface_address):
            missed_checks = 0
        else:
            missed_checks += 1
            print(
                "WiFi disconnect check {}/{} failed for {} at {}".format(
                    missed_checks,
                    WIFI_DISCONNECT_CHECKS,
                    interface_name,
                    interface_address,
                )
            )
            if missed_checks >= WIFI_DISCONNECT_CHECKS:
                on_disconnect()
                return


def create_wpad_server(hhost, hport, phost, pport):
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class HTTPHandler(BaseHTTPRequestHandler):
        def do_HEAD(s):
            s.send_response(200)
            s.send_header("Content-type", "application/x-ns-proxy-autoconfig")
            s.end_headers()

        def do_GET(s):
            s.send_response(200)
            s.send_header("Content-type", "application/x-ns-proxy-autoconfig")
            s.end_headers()
            s.wfile.write(
                (
                    """
function FindProxyForURL(url, host)
{
   if (isInNet(host, "192.168.0.0", "255.255.0.0")) {
      return "DIRECT";
   } else if (isInNet(host, "172.16.0.0", "255.240.0.0")) {
      return "DIRECT";
   } else if (isInNet(host, "10.0.0.0", "255.0.0.0")) {
      return "DIRECT";
   } else {
      return "SOCKS5 %s:%d; SOCKS %s:%d";
   }
}
"""
                    % (phost, pport, phost, pport)
                )
                .lstrip()
                .encode()
            )

    HTTPServer.allow_reuse_address = True
    server = HTTPServer((hhost, hport), HTTPHandler)
    return server


def run_wpad_server(server):
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    background_audio = BackgroundAudio(test_tone=BACKGROUND_AUDIO_TEST_TONE)
    background_audio_enabled = KEEP_ALIVE_WITH_AUDIO and background_audio.start()

    wpad_server = create_wpad_server(LISTEN_HOST, WPAD_PORT, PROXY_HOST, SOCKS_PORT)

    if background_audio_enabled:
        audio_mode = "440 Hz test tone" if BACKGROUND_AUDIO_TEST_TONE else "silence"
        if background_audio.player_backend == "Pyto BackgroundTask":
            session_mode = "Pyto background task"
        elif background_audio.native_session_active:
            session_mode = "native playback session"
        else:
            session_mode = "Pythonista player only"
        initial_output += "Background audio enabled ({}, {}, {})\n".format(
            audio_mode, session_mode, background_audio.player_backend
        )
        if background_audio.host_supports_background_audio is False:
            initial_output += (
                "Warning: Pythonista does not declare the iOS audio background mode; "
                "playback will pause when the app leaves the foreground.\n"
            )
        elif background_audio.host_supports_background_audio is True:
            initial_output += "Host app declares the iOS audio background mode\n"
    elif KEEP_ALIVE_WITH_AUDIO:
        initial_output += "Background audio keep-alive unavailable: {}\n".format(
            background_audio.error
        )

    initial_output += "PAC URL: http://{}:{}/wpad.dat\n".format(PROXY_HOST, WPAD_PORT)
    initial_output += "SOCKS Address: {}:{}\n".format(
        PROXY_HOST or LISTEN_HOST, SOCKS_PORT
    )
    initial_output += "HTTP Proxy Address: {}:{}\n".format(
        PROXY_HOST or LISTEN_HOST, HTTP_PORT
    )
    if EXIT_ON_WIFI_DISCONNECT and wifi_interface_name and wifi_interface_address:
        initial_output += (
            "Auto-stop: watching {} on {} at {}\n".format(
                WIFI_NETWORK_NAME,
                wifi_interface_name,
                wifi_interface_address,
            )
        )
    elif EXIT_ON_WIFI_DISCONNECT:
        initial_output += (
            "Warning: auto-stop is enabled, but no WiFi connection was found "
            "at startup\n"
        )
    stats = StatusMonitor(initial_output)
    root_logger = logging.getLogger()
    root_logger.addHandler(stats)

    thread = threading.Thread(target=run_wpad_server, args=(wpad_server,))
    thread.daemon = True
    thread.start()

    async def main():
        socks_server = AsyncProxyServer(
            AsyncSocks5Handler,
            listen_hosts=LISTEN_HOST,
            listen_port=SOCKS_PORT,
            traffic_stats=stats,
            resolver=resolver,
            connect_host_ipv4=CONNECT_HOST_IPV4,
            connect_host_ipv6=CONNECT_HOST_IPV6,
        )
        http_server = AsyncProxyServer(
            AsyncHTTPProxyHandler,
            listen_hosts=LISTEN_HOST,
            listen_port=HTTP_PORT,
            traffic_stats=stats,
            resolver=resolver,
            connect_host_ipv4=CONNECT_HOST_IPV4,
            connect_host_ipv6=CONNECT_HOST_IPV6,
        )
        await asyncio.gather(socks_server.start(), http_server.start())
        stats_task = asyncio.create_task(stats.render_forever())
        shutdown_event = asyncio.Event()
        monitor_stop_event = threading.Event()
        monitor_thread = None
        if (
            EXIT_ON_WIFI_DISCONNECT
            and wifi_interface_name
            and wifi_interface_address
        ):
            loop = asyncio.get_running_loop()

            def request_wifi_shutdown():
                loop.call_soon_threadsafe(shutdown_event.set)

            monitor_thread = threading.Thread(
                target=monitor_wifi_connection,
                args=(
                    wifi_interface_name,
                    wifi_interface_address,
                    monitor_stop_event,
                    request_wifi_shutdown,
                ),
                name="wifi-disconnect-monitor",
                daemon=True,
            )
            monitor_thread.start()

        try:
            await shutdown_event.wait()
            print(
                "WiFi network {} disconnected; shutting down server.".format(
                    WIFI_NETWORK_NAME
                )
            )
        finally:
            monitor_stop_event.set()
            await asyncio.gather(socks_server.close(), http_server.close())
            stats_task.cancel()
            await asyncio.gather(stats_task, return_exceptions=True)
            if monitor_thread is not None:
                monitor_thread.join(timeout=WIFI_CHECK_INTERVAL + 1)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Shutting down.")
    finally:
        wpad_server.shutdown()
        wpad_server.server_close()
        thread.join(timeout=2)
        background_audio.stop()
        root_logger.removeHandler(stats)
        stats.close()
