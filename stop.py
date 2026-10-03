#!python3
"""Run in Pyto to request proxy cleanup, leaving the Pyto process running."""

import socket


CONTROL_KEY = "ios_socks_server.control.v1"


def request_stop():
    try:
        import userkeys
    except ImportError:
        print("Run stop.py in Pyto on the same iPhone as the proxy.")
        return False
    try:
        control = userkeys.get(CONTROL_KEY)
    except KeyError:
        print("No proxy control listener found. Start the updated socks5.py first.")
        return False
    except Exception as error:
        print("Could not read proxy control:", error)
        return False
    if (not isinstance(control, dict)
            or type(control.get("port")) is not int
            or not 0 < control["port"] <= 65535
            or not isinstance(control.get("token"), str)
            or len(control["token"]) != 48
            or any(character not in "0123456789abcdef" for character in control["token"])):
        print("Invalid proxy control record. Restart socks5.py.")
        return False
    try:
        # Never connect to a host supplied by shared storage; control is local.
        with socket.create_connection(("127.0.0.1", control["port"]), timeout=2) as client:
            client.sendall(control["token"].encode("ascii") + b"\n")
            with client.makefile("rb") as reader:
                response = reader.readline(256)
        if response != b"STOP REQUESTED\n":
            print("Proxy rejected the stop request. Restart socks5.py.")
            return False
    except OSError as error:
        print("Could not reach the proxy stop listener:", error)
        return False
    print("Proxy stop requested. The server will close its sockets and audio; Pyto stays open.")
    return True


if __name__ in ("__main__", "<run_path>", "widget"):
    request_stop()
