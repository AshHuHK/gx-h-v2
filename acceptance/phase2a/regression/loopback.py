"""Execute an acceptance command inside a fresh network namespace with real, reject-only Mihomo."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import time


CONFIG = """port: 7890
socks-port: 7891
bind-address: 127.0.0.1
allow-lan: false
mode: rule
log-level: warning
ipv6: false
geo-auto-update: false
disable-keep-alive: true
dns:
  enable: false
rules:
  - MATCH,REJECT
"""


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def local_listeners():
    ports = {}
    for port in (7890, 7891):
        with socket.socket() as probe:
            probe.settimeout(.3)
            ports[str(port)] = probe.connect_ex(("127.0.0.1", port)) == 0
    return ports


def run(args):
    identity = os.readlink("/proc/self/ns/net")
    assert identity != args.outer_namespace
    # No network device or default route may carry this fixture's traffic off host.
    interfaces = dict(socket.if_nameindex())
    assert set(interfaces.values()) <= {"lo", "tunl0"}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        request = struct.pack("16sH14x", b"lo", 0)
        flags = struct.unpack("16sH14x", fcntl.ioctl(sock, 0x8913, request))[1]
        fcntl.ioctl(sock, 0x8914, struct.pack("16sH14x", b"lo", flags | 1))
        for dev in interfaces.values():
            data = fcntl.ioctl(sock, 0x8913, struct.pack("16sH14x", dev.encode(), 0))
            assert bool(struct.unpack("16sH14x", data)[1] & 1) == (dev == "lo")
    assert len(Path("/proc/net/route").read_text().splitlines()) <= 1
    assert not any(local_listeners().values())
    ipv6_interfaces = {row.split()[-1] for row in Path("/proc/net/ipv6_route").read_text().splitlines()}
    assert ipv6_interfaces <= {"lo"}
    with tempfile.TemporaryDirectory(prefix="gx-phase2a-mihomo-") as directory:
        work = Path(directory)
        config = work / "offline.yaml"
        config.write_text(CONFIG)
        with (work / "server.log").open("w+") as log, subprocess.Popen(
                [args.clash, "-d", work, "-f", config], stdout=log, stderr=subprocess.STDOUT) as proxy:
            try:
                until = time.monotonic() + 8
                while not all(local_listeners().values()):
                    if proxy.poll() is not None or time.monotonic() > until:
                        log.seek(0)
                        raise RuntimeError("isolated Mihomo startup failed: " + log.read()[-800:])
                    time.sleep(.05)
                expected = {key: "http://127.0.0.1:7890" for key in
                            ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY")}
                expected.update(ALL_PROXY="socks5h://127.0.0.1:7891", all_proxy="socks5h://127.0.0.1:7891")
                assert all(os.environ.get(key) == value for key, value in expected.items())
                for key in ("NO_PROXY", "no_proxy"):
                    bypass = set(os.environ[key].split(","))
                    assert bypass and bypass <= {"localhost", "127.0.0.1", "::1"}
                receipt = {"outer_namespace": args.outer_namespace, "fixture_namespace": identity,
                           "interfaces": interfaces, "ipv4_routes": [], "ipv6_route_interfaces": sorted(ipv6_interfaces),
                           "proxy_program": subprocess.check_output([args.clash, "-v"], text=True).splitlines()[0],
                           "proxy_program_sha256": sha(args.clash), "proxy_config": CONFIG,
                           "outer_proxy_listeners": json.loads(args.outer_listeners),
                           "isolated_proxy_listeners": local_listeners(), "original_proxy_environment": True,
                           "command": args.command, "started_unix": time.time()}
                args.receipt.write_text(json.dumps(receipt, indent=2) + "\n")
                completed = subprocess.run(args.command)
                receipt.update(exit_code=completed.returncode, finished_unix=time.time())
                args.receipt.write_text(json.dumps(receipt, indent=2) + "\n")
                return completed.returncode
            finally:
                proxy.terminate()
                try:
                    proxy.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proxy.kill()
                    proxy.wait()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--clash", type=Path, required=True)
    p.add_argument("--receipt", type=Path, required=True)
    p.add_argument("--outer-namespace")
    p.add_argument("--outer-listeners")
    p.add_argument("command", nargs=argparse.REMAINDER)
    args = p.parse_args()
    if args.command[:1] == ["--"]:
        args.command.pop(0)
    if not args.command:
        p.error("an explicit command is required")
    if args.outer_namespace is None:
        argv = ["unshare", "--user", "--map-root-user", "--net", sys.executable, __file__,
                "--clash", str(args.clash.resolve()), "--receipt", str(args.receipt.resolve()),
                "--outer-namespace", os.readlink("/proc/self/ns/net"),
                "--outer-listeners", json.dumps(local_listeners()), "--", *args.command]
        os.execvp("unshare", argv)
    sys.exit(run(args))
