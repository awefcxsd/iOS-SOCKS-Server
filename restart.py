#!python3
"""Ask the running proxy to close and reopen its services, keeping Pyto open."""

import socket


CONTROL_KEY = "ios_socks_server.control.v1"


def request_restart():
    try:
        import userkeys
    except ImportError:
        print("Run restart.py in Pyto on the same iPhone as the proxy.")
        return False
    try:
        control = userkeys.get(CONTROL_KEY)
    except KeyError:
        print("No running proxy found. Start the updated socks5.py first.")
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
        print("Invalid proxy control record. Rerun socks5.py.")
        return False
    if control.get("restart_supported") is not True:
        print("This proxy does not support restart.py. Run the updated socks5.py first.")
        return False
    try:
        with socket.create_connection(("127.0.0.1", control["port"]), timeout=2) as client:
            client.sendall(b"RESTART " + control["token"].encode("ascii") + b"\n")
            with client.makefile("rb") as reader:
                response = reader.readline(256)
        if response != b"RESTART REQUESTED\n":
            print("Proxy rejected the restart request or is already shutting down. Try again shortly.")
            return False
    except OSError as error:
        print("Could not reach the proxy control listener:", error)
        return False
    print("Proxy restart requested. Existing connections will close, then the server will start again.")
    return True


if __name__ in ("__main__", "<run_path>", "widget"):
    request_restart()
