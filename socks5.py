#!python3
# Socks5/HTTP Proxy server for Pythonista by @nneonneo
# Pretty statistics view added by @philrosenthal; networking uses IPv4 only.

import asyncio
import logging
import socket
import threading

from proxy_lib.background_audio import BackgroundAudio
from proxy_lib.http_proxy_server import AsyncHTTPProxyHandler
from proxy_lib.lifecycle import (
    PytoStopWatcher, cleanup_steps, run_until_stopped, service_thread, stop_wpad_server,
)
from proxy_lib.proxy_server import AsyncProxyServer
from proxy_lib.socks5_server import AsyncSocks5Handler
from proxy_lib.status import StatusMonitor

logging.basicConfig(level=logging.ERROR)

# IP over which the proxy will be available (probably WiFi IP)
PROXY_HOST = "172.20.10.1"
# IP over which the proxy will attempt to connect to the Internet
CONNECT_HOST_IPV4 = None
# Use iOS routing directly instead of automatically binding cellular/VPN IPs.
# Useful when default-route HTTPS works but a bound route fails in diagnostics.
USE_SYSTEM_DEFAULT_ROUTE = False
# Refresh automatically selected cellular/VPN addresses before new requests.
REFRESH_SOURCE_ADDRESSES = True
# Time out connections after being idle for this long (in seconds)
IDLE_TIMEOUT = 1800

LISTEN_HOST = "0.0.0.0"
SOCKS_PORT = 9876
HTTP_PORT = 9877
WPAD_PORT = 8088

USE_PHONE_VPN = False
CUSTOM_RESOLVERS = []
# Loop silent audio while running so Pythonista can continue executing after
# iOS sends it to the background. iOS may still suspend or terminate the app.
KEEP_ALIVE_WITH_AUDIO = True
# Play a quiet 440 Hz tone instead of silence to verify background playback.
BACKGROUND_AUDIO_TEST_TONE = False

# Stop the server when the WiFi connection used at startup goes away. iOS does
# not reliably expose the SSID to Pyto, so this name is a label for the network
# that must be connected when the script starts.
EXIT_ON_WIFI_DISCONNECT = False
WIFI_NETWORK_NAME = "Subaru_5G"
WIFI_CHECK_INTERVAL = 2
WIFI_DISCONNECT_CHECKS = 3
IFF_UP = 0x1
IFF_RUNNING = 0x40
CONNECTIVITY_TEST_TIMEOUT = 5
IPV4_TEST_ADDRESS = ("1.1.1.1", 80)

# Try to keep the screen from turning off (iOS)
try:
    import console
    from objc_util import on_main_thread

    on_main_thread(console.set_idle_timer_disabled)(True)
except ImportError:
    pass


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
    wifi_interface_name = None
    wifi_interface_address = None
    connect_interface_ipv4 = None

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

    if USE_SYSTEM_DEFAULT_ROUTE:
        CONNECT_HOST_IPV4 = None
        ipv4_output += "Will use the system default route (source binding disabled)\n"
    elif iftypes["cell"]:
        iface_ipv4 = next(
            (iface for iface in iftypes["cell"] if iface.addr.family == socket.AF_INET),
            None,
        )

        if iface_ipv4:
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
                connect_interface_ipv4 = iface_ipv4.name
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

    initial_output += ipv4_output + "IPv4 only (IPv6 disabled)\n"
    print(initial_output)
except Exception as e:
    logging.error("Address detection failed: %s: %s", type(e).__name__, e)
    import traceback

    traceback.print_exc()

    interfaces = None
    wifi_interface_name = None
    wifi_interface_address = None
    connect_interface_ipv4 = None


def current_source_addresses():
    """Refresh selected interfaces without silently changing the outbound route."""
    from proxy_lib import ifaddrs

    active_interfaces = [
        iface for iface in ifaddrs.get_interfaces()
        if iface.addr and iface.flags & IFF_UP and iface.flags & IFF_RUNNING
    ]

    def address_for(interface_name, family, configured_address):
        if interface_name is None:
            return configured_address
        addresses = [
            iface.addr.address for iface in active_interfaces
            if iface.name == interface_name and iface.addr.family == family
        ]
        if configured_address in addresses:
            return configured_address
        if not addresses:
            return None
        return addresses[0]

    addresses = (
        address_for(connect_interface_ipv4, socket.AF_INET, CONNECT_HOST_IPV4),
        None,
    )
    if addresses == (None, None):
        raise OSError(
            "Selected outbound interfaces have no active addresses; "
            "reconnect cellular/VPN or rerun the script if the interface changed"
        )
    return addresses


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
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class WPADServer(ThreadingHTTPServer):
        daemon_threads = True
        block_on_close = False
        allow_reuse_address = True

        def process_request(self, request, client_address):
            service_thread(
                target=self.process_request_thread,
                args=(request, client_address), daemon=True,
            ).start()

        def get_request(self):
            request, address = super().get_request()
            request.settimeout(1)
            return request, address

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

    server = WPADServer((hhost, hport), HTTPHandler)
    return server


def run_wpad_server(server):
    try:
        server.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass


def run():
    global initial_output
    background_audio = BackgroundAudio(test_tone=BACKGROUND_AUDIO_TEST_TONE)
    wpad_server = None
    thread = None
    stats = None
    root_logger = logging.getLogger()
    proxy_servers = []
    monitor_stop_event = threading.Event()
    stop_watcher = None

    def emergency_stop_services():
        # Pyto's native Stop can park the owning thread without running finally.
        # This callback runs independently and must not rely on asyncio callbacks.
        steps = [monitor_stop_event.set]
        steps.extend(server.emergency_stop for server in proxy_servers)
        steps.append(background_audio.stop)
        if wpad_server is not None:
            steps.append(lambda: stop_wpad_server(wpad_server, thread))
        cleanup_steps(*steps)
        print("Pyto Stop detected; proxy sockets and background audio stopped.")

    def stop_services():
        steps = [monitor_stop_event.set]
        if stop_watcher is not None:
            steps.append(stop_watcher.stop)
        steps.extend(server.stop_now for server in proxy_servers)
        steps.append(background_audio.stop)
        if wpad_server is not None:
            steps.append(lambda: stop_wpad_server(wpad_server, thread))
        if stats is not None:
            steps.extend((lambda: root_logger.removeHandler(stats), stats.close))
        cleanup_steps(*steps)

    try:
        stop_watcher = PytoStopWatcher(emergency_stop_services)
        if stop_watcher.start():
            initial_output += "Pyto native Stop watcher enabled (shutdown v2)\n"
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

        thread = service_thread(target=run_wpad_server, args=(wpad_server,))
        thread.daemon = True
        thread.start()

        async def main():
            source_address_provider = (
                current_source_addresses
                if REFRESH_SOURCE_ADDRESSES
                and connect_interface_ipv4
                else None
            )
            socks_server = AsyncProxyServer(
                AsyncSocks5Handler,
                listen_hosts=LISTEN_HOST,
                listen_port=SOCKS_PORT,
                traffic_stats=stats,
                resolver=resolver,
                connect_host_ipv4=CONNECT_HOST_IPV4,
                source_address_provider=source_address_provider,
            )
            http_server = AsyncProxyServer(
                AsyncHTTPProxyHandler,
                listen_hosts=LISTEN_HOST,
                listen_port=HTTP_PORT,
                traffic_stats=stats,
                resolver=resolver,
                connect_host_ipv4=CONNECT_HOST_IPV4,
                source_address_provider=source_address_provider,
            )
            proxy_servers.extend((socks_server, http_server))
            await asyncio.gather(socks_server.start(), http_server.start())
            stats_task = asyncio.create_task(stats.render_forever())
            shutdown_event = asyncio.Event()
            monitor_thread = None
            if (
                EXIT_ON_WIFI_DISCONNECT
                and wifi_interface_name
                and wifi_interface_address
            ):
                loop = asyncio.get_running_loop()

                def request_wifi_shutdown():
                    loop.call_soon_threadsafe(shutdown_event.set)

                monitor_thread = service_thread(
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
                stop_services()
                stats_task.cancel()
                await asyncio.gather(
                    socks_server.close(), http_server.close(), stats_task,
                    return_exceptions=True,
                )
                if monitor_thread is not None:
                    monitor_thread.join(timeout=WIFI_CHECK_INTERVAL + 1)

        try:
            run_until_stopped(main(), stop_services)
        except (KeyboardInterrupt, SystemExit):
            print("Shutting down.")
    finally:
        stop_services()


if __name__ == "__main__":
    run()
