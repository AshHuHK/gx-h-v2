"""A byte relay for severing one GX client while its official stdio host keeps running."""
from contextlib import ExitStack, suppress
import json
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time


def bridge(path):
    """Use the ordinary GX AppServerClient stdio codec on both sides of the relay."""
    with socket.socket(socket.AF_UNIX) as conn:
        conn.connect(str(path))

        def send():
            try:
                for line in sys.stdin.buffer:
                    conn.sendall(line)
            finally:
                conn.shutdown(socket.SHUT_RDWR)

        threading.Thread(target=send, daemon=True).start()
        with conn.makefile("rb") as incoming:
            for line in incoming:
                sys.stdout.buffer.write(line)
                sys.stdout.buffer.flush()


class Wire:
    def __init__(self, directory, binary, env, out, socket_path):
        self.frames = []
        self.errors = []
        self.closed = False
        self.root = None
        self.out = out
        with ExitStack() as startup:
            self.listener = startup.enter_context(socket.socket(socket.AF_UNIX))
            self.listener.bind(str(socket_path))
            self.listener.listen(1)
            self.log = startup.enter_context((out / "official-host.log").open("w"))
            self.process = subprocess.Popen([str(binary), "app-server", "--listen", "stdio://"],
                                           env=env, cwd=directory, stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE, stderr=self.log)
            startup.callback(self.process.stdout.close)
            startup.callback(self._stop_host)
            self.reader = threading.Thread(target=self._session, daemon=True)
            self.reader.start()
            startup.pop_all()  # The fixture now owns these resources until shutdown.

    def _session(self):
        try:
            conn, _ = self.listener.accept()
            with conn:
                writer = threading.Thread(target=self._requests, args=(conn,), daemon=True)
                writer.start()
                with (self.out / "official-wire.jsonl").open("w", buffering=1) as raw:
                    for line in self.process.stdout:
                        data = json.loads(line)
                        method, params = data.get("method"), data.get("params") or {}
                        item = params.get("item") or {}
                        if method == "turn/started" and not self.root:
                            self.root = params["threadId"]
                        disposition = "gx_offline" if self.closed else "deliver"
                        if method in ("item/started", "item/completed") and item.get("type") == "subAgentActivity" and item.get("kind") == "started":
                            disposition = "drop_spawn"
                        if method == "turn/started" and params["threadId"] != self.root:
                            disposition = "withhold_child_start"
                        if disposition == "deliver":
                            try:
                                conn.sendall(line)
                            except OSError:
                                self.closed = True
                                disposition = "gx_offline"
                        if disposition == "drop_spawn" or method in ("turn/started", "turn/completed", "rawResponse/completed", "thread/tokenUsage/updated"):
                            row = {"ns": time.monotonic_ns(), "disposition": disposition, "frame": data}
                            raw.write(json.dumps(row) + "\n")
                            self.frames.append(row)
        except Exception as exc:
            self.errors.append(repr(exc))

    def _requests(self, conn):
        try:
            with conn.makefile("rb") as source:
                for line in source:
                    self.process.stdin.write(line)
                    self.process.stdin.flush()
        except (OSError, ValueError):
            pass
        finally:
            self.closed = True

    def _stop_host(self):
        with suppress(BrokenPipeError):
            self.process.stdin.close()
        try:
            self.process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()

    def shutdown(self):
        if self.log.closed:
            return
        try:
            self._stop_host()
        finally:
            with suppress(OSError):
                self.listener.shutdown(socket.SHUT_RDWR)
            self.listener.close()
            self.reader.join(timeout=3)
            self.process.stdout.close()
            self.log.close()


if __name__ == "__main__":
    bridge(Path(sys.argv[1]))
