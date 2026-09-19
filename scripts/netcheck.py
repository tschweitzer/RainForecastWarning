#!/usr/bin/env python
"""Time the same DWD download through each layer, to find which one is slow.

curl fetches an RV archive in about a second; the backfill takes many times that from the same
machine. Something between those two is responsible, and guessing which has not worked. This
fetches the identical URL four ways and prints the times side by side:

    socket      a bare TCP+TLS connect, no HTTP - measures connection setup alone
    urllib      the standard library
    httpx       the library our client is built on, with nothing of ours around it
    DWDClient   ours, exactly as backfill calls it

Read it as a ladder. The first layer that is slow is the one at fault, and everything below it is
exonerated. `--ipv4` forces A records: Python connects to resolved addresses in order and waits
out a full TCP timeout on a dead one, where curl's Happy Eyeballs gives up after ~200 ms - so a
host with a broken IPv6 path is fast in curl and slow in Python, with a fixed cost per connection
that no payload size explains.
"""

from __future__ import annotations

import argparse
import socket
import ssl
import sys
import time
import urllib.request

URL = "https://opendata.dwd.de/weather/radar/composite/rv/DE1200_RV_LATEST.tar.bz2"
HOST = "opendata.dwd.de"


#: Captured before --ipv4 replaces it, so the DNS report always says what the resolver really
#: returns rather than what the flag has forced.
_real_getaddrinfo = socket.getaddrinfo


def addresses() -> None:
    print(f"DNS for {HOST}:")
    for family, name in ((socket.AF_INET, "A   "), (socket.AF_INET6, "AAAA")):
        try:
            got = {a[4][0] for a in _real_getaddrinfo(HOST, 443, family, socket.SOCK_STREAM)}
            print(f"  {name} {', '.join(sorted(got)) or '-'}")
        except socket.gaierror as exc:
            print(f"  {name} none ({exc.strerror})")


def time_socket(family: int | None) -> None:
    label = {socket.AF_INET: "socket v4", socket.AF_INET6: "socket v6"}.get(family, "socket    ")
    started = time.monotonic()
    try:
        infos = socket.getaddrinfo(HOST, 443, family or 0, socket.SOCK_STREAM)
        af, kind, proto, _, addr = infos[0]
        raw = socket.socket(af, kind, proto)
        raw.settimeout(30)
        raw.connect(addr)
        tcp = time.monotonic() - started
        ctx = ssl.create_default_context()
        with ctx.wrap_socket(raw, server_hostname=HOST):
            pass
        print(f"  {label}  tcp {tcp:5.2f}s  tls {time.monotonic() - started:5.2f}s  -> {addr[0]}")
    except OSError as exc:
        print(f"  {label}  failed after {time.monotonic() - started:5.2f}s: {exc}")


def timed(label: str, fn) -> None:
    started = time.monotonic()
    try:
        size = fn()
        elapsed = time.monotonic() - started
        rate = size / elapsed / 1000 if elapsed else 0
        print(f"  {label:<10} {elapsed:6.2f}s  {size:>8} B  {rate:6.0f} KB/s")
    except Exception as exc:  # noqa: BLE001 - a diagnostic prints what went wrong
        print(f"  {label:<10} failed after {time.monotonic() - started:6.2f}s: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-n", "--repeat", type=int, default=3)
    parser.add_argument("--ipv4", action="store_true", help="force A records everywhere")
    args = parser.parse_args()

    addresses()

    if args.ipv4:

        def v4_only(host, port, family=0, *rest):
            return _real_getaddrinfo(host, port, socket.AF_INET, *rest)

        socket.getaddrinfo = v4_only  # type: ignore[assignment]
        print("\nforcing IPv4")
    print("\nconnection setup only:")
    time_socket(socket.AF_INET)
    if not args.ipv4:
        time_socket(socket.AF_INET6)

    import httpx

    from rainalert.radar.client import DWDClient

    client = DWDClient(
        base_url="https://opendata.dwd.de/weather/radar/composite/rv/",
        latest_name="DE1200_RV_LATEST.tar.bz2",
        user_agent="RainAlert/0.1 (netcheck)",
        max_response_bytes=32 * 1024 * 1024,
        hourly_byte_budget=1024 * 1024 * 1024,
        daily_byte_budget=8 * 1024 * 1024 * 1024,
    )
    for round_ in range(1, args.repeat + 1):
        print(f"\nround {round_}:")
        timed("urllib", lambda: len(urllib.request.urlopen(URL, timeout=30).read()))
        timed("httpx", lambda: len(httpx.get(URL, timeout=30, follow_redirects=False).content))
        timed("DWDClient", lambda: len(client.fetch_latest().body or b""))
    client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
