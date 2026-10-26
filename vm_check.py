#!/usr/bin/env python3
"""Connectivity check for the payout/SMS VM. Read-only: it never claims or changes a payout.

Run on the VM:
  python3 vm_check.py
  python3 vm_check.py --target smsc.example.net:2775 --target api.partner.example:443+tls
  python3 vm_check.py --poll 5            # 5 read-only polls of the payout queue

Environment (optional):
  SUPABASE_URL           checked automatically (HTTPS)
  CODEGEN_DATABASE_URL   or DATABASE_URL: checked and used for the database test and --poll

A target is host:port. Add +tls to also check the TLS certificate.
Exit code is 0 only if every check passes.
"""
import argparse
import os
import socket
import ssl
import sys
import time
import urllib.parse
import urllib.request

results = []


def report(ok, kind, text):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {kind:<6} {text}")


def info(kind, text):
    print(f"INFO  {kind:<6} {text}")


def check_target(host, port, tls, timeout, insecure):
    try:
        addrs = sorted({a[4][0] for a in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)})
    except socket.gaierror as e:
        report(False, "dns", f"{host}: {e}")
        return
    report(True, "dns", f"{host} -> {', '.join(addrs)}")

    try:
        t0 = time.monotonic()
        sock = socket.create_connection((host, port), timeout=timeout)
        ms = (time.monotonic() - t0) * 1000
    except OSError as e:
        report(False, "tcp", f"{host}:{port}: {e}")
        return
    report(True, "tcp", f"{host}:{port} connected in {ms:.0f} ms")

    if tls:
        try:
            ctx = ssl.create_default_context()
            with ctx.wrap_socket(sock, server_hostname=host) as s:
                cert = s.getpeercert()
                subject = dict(x[0] for x in cert.get("subject", ()))
                report(True, "tls", f"{host}: {s.version()}, subject {subject.get('commonName', '?')}, expires {cert.get('notAfter')}")
        except ssl.SSLError as e:
            report(False, "tls", f"{host}: {e} (is the partner's CA installed on this VM?)")
            if insecure:
                try:
                    c2 = ssl.create_default_context()
                    c2.check_hostname = False
                    c2.verify_mode = ssl.CERT_NONE
                    with c2.wrap_socket(socket.create_connection((host, port), timeout=timeout)) as s:
                        info("tls", f"{host}: handshake works without verification ({s.version()}). Diagnosis only.")
                except OSError as e2:
                    info("tls", f"{host}: handshake also fails unverified: {e2}")
        except OSError as e:
            report(False, "tls", f"{host}: {e}")
    else:
        sock.close()


def egress_ip():
    try:
        with urllib.request.urlopen("https://checkip.amazonaws.com", timeout=5) as r:
            info("egress", f"internet traffic leaves from {r.read().decode().strip()} (should be the NAT gateway's address)")
    except Exception as e:
        info("egress", f"could not look up the public address: {e}")


def database(dsn, polls):
    try:
        import psycopg
    except ImportError:
        info("db", "psycopg not installed, skipping (pip install 'psycopg[binary]')")
        return
    try:
        with psycopg.connect(dsn, connect_timeout=10) as conn:
            now = conn.execute("select now()").fetchone()[0]
            report(True, "db", f"connected, server time {now}")
            for i in range(polls):
                n = conn.execute("""select count(*) from public.payout
                                    where status = 'pending' and next_attempt_at <= now()""").fetchone()[0]
                print(f"      poll {i + 1}/{polls}: {n} payout(s) due (read-only)")
                conn.commit()
                if i < polls - 1:
                    time.sleep(2)
    except Exception as e:
        report(False, "db", str(e).splitlines()[0])


def parse_target(text):
    tls = text.endswith("+tls")
    text = text[:-4] if tls else text
    host, _, port = text.rpartition(":")
    if not host or not port.isdigit():
        sys.exit(f"Bad target '{text}'. Use host:port or host:port+tls")
    return host, int(port), tls


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", action="append", default=[], help="host:port or host:port+tls (repeatable)")
    ap.add_argument("--timeout", type=float, default=8.0)
    ap.add_argument("--poll", type=int, default=0, help="run N read-only polls of the payout queue")
    ap.add_argument("--insecure-tls", action="store_true", help="if TLS verification fails, also try without it (diagnosis only)")
    a = ap.parse_args()

    print(f"Host {socket.gethostname()}, Python {sys.version.split()[0]}")
    egress_ip()

    targets = [parse_target(t) for t in a.target]
    sb = os.environ.get("SUPABASE_URL")
    dsn = os.environ.get("CODEGEN_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if sb:
        targets.insert(0, (urllib.parse.urlparse(sb).hostname, 443, True))
    if dsn:
        p = urllib.parse.urlparse(dsn)
        targets.insert(0, (p.hostname, p.port or 5432, False))
    if not targets:
        print("No targets. Pass --target, or set SUPABASE_URL / CODEGEN_DATABASE_URL.")
    for host, port, tls in targets:
        check_target(host, port, tls, a.timeout, a.insecure_tls)

    if dsn:
        database(dsn, a.poll)
    elif a.poll:
        info("db", "no database URL set, cannot poll")

    failed = results.count(False)
    print(f"\n{len(results) - failed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()