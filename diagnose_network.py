"""Run in Pyto while the proxy is failing; compare default and bound routes."""

import socket
import time
import urllib.request

from dns.resolver import Resolver

TIMEOUT = 5
DNS_SERVERS = ["1.1.1.1", "8.8.8.8"]


def report(label, operation):
    started = time.monotonic()
    try:
        result = operation()
        print("PASS {} ({:.1f}s): {}".format(label, time.monotonic() - started, result))
    except Exception as error:
        print("FAIL {} ({:.1f}s): {}: {}".format(
            label, time.monotonic() - started, type(error).__name__, error
        ))


def tcp_probe(family, source, destination):
    with socket.socket(family, socket.SOCK_STREAM) as connection:
        connection.settimeout(TIMEOUT)
        if source:
            connection.bind((source, 0))
        connection.connect((destination, 443))
        return "connected from {}".format(connection.getsockname()[0])


def dns_probe(source, nameserver):
    resolver = Resolver(configure=False)
    resolver.nameservers = [nameserver]
    resolver.timeout = 2
    resolver.lifetime = TIMEOUT
    return ", ".join(str(answer) for answer in resolver.resolve(
        "example.com", "A", source=source
    ))


def https_probe():
    # Ignore environment proxy settings: test the phone's default route directly.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open("https://example.com/", timeout=TIMEOUT) as response:
        return "HTTP {}".format(response.status)


def main():
    print("Run this while the failure is present, before rebooting.\n")
    report("HTTPS using system DNS and default route", https_probe)
    sources = [("default IPv4 route", socket.AF_INET, None),
               ("default IPv6 route", socket.AF_INET6, None)]
    try:
        from proxy_lib.ifaddrs import get_interfaces

        seen = set()
        for interface in get_interfaces():
            if (not interface.addr or not interface.name.startswith(("pdp_ip", "utun"))
                    or not interface.flags & 0x1 or not interface.flags & 0x40):
                continue
            family, address = interface.addr
            if family not in (socket.AF_INET, socket.AF_INET6):
                continue
            if family == socket.AF_INET6 and address.startswith("fe80:"):
                continue
            key = (interface.name, family, address)
            if key in seen:
                continue
            seen.add(key)
            sources.append(("{} {}".format(interface.name, address), family, address))
    except Exception as error:
        print("Could not enumerate iOS interfaces: {}".format(error))

    for label, family, source in sources:
        destination = "1.1.1.1" if family == socket.AF_INET else "2606:4700:4700::1111"
        report("{}: TCP to {}:443".format(label, destination),
               lambda: tcp_probe(family, source, destination))
        nameservers = DNS_SERVERS if family == socket.AF_INET else ["2606:4700:4700::1111"]
        for nameserver in nameservers:
            report("{}: DNS via {}".format(label, nameserver),
                   lambda: dns_probe(source, nameserver))
    print("\nIf default HTTPS works but bound TCP fails, source binding is suspect.")
    print("If TCP works but DNS fails, investigate DNS reachability or VPN DNS policy.")
    print("These tests cover specific destinations; one failed probe does not prove a route is down.")


if __name__ == "__main__":
    main()
