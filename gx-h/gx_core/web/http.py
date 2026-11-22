"""Serve a single local project through the original conversation endpoints."""

import argparse
import json
import re
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .. import contracts
from ..errors import GxError
from . import projects

BASE = "/api/science/conversations"
STATIC = {"index.html": "text/html", "app.js": "text/javascript", "site.css": "text/css"}


class Handler(BaseHTTPRequestHandler):
    def send_bytes(self, status, content, mime):
        self.send_response(status)
        self.send_header("Content-Type", mime + "; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'; object-src 'none'")
        self.end_headers()
        self.wfile.write(content)

    def send_json(self, status, body):
        self.send_bytes(status, json.dumps(body, ensure_ascii=False).encode(), "application/json")

    def body(self):
        if self.headers.get_content_type() != "application/json":
            raise ValueError("Use application/json")
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 < size <= 512 * 1024:
            raise ValueError("Request size outside 1–524288 bytes")
        obj = json.loads(self.rfile.read(size))
        if not isinstance(obj, dict):
            raise ValueError("Expected a JSON object")
        return obj

    def stream(self, after):
        if not 0 <= after <= 2**63 - 1:
            raise ValueError("Invalid event cursor")
        app = self.server
        # Query before sending the HTTP status so store errors get a real error response.
        rows = projects.milestones(app.state, app.project, after)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(b"retry: 1500\n: connected\n\n")
        deadline = time.monotonic() + 25
        while True:
            for row in rows:
                after = row["id"]
                self.wfile.write(f"id: {after}\ndata: {json.dumps(row)}\n\n".encode())
            if not rows:
                self.wfile.write(b": keep-alive\n\n")
            self.wfile.flush()
            if time.monotonic() >= deadline:
                return
            if len(rows) < 200:
                time.sleep(1)
            try:
                rows = projects.milestones(app.state, app.project, after)
            except sqlite3.Error:
                return  # Browser retries with its last delivered event ID.

    def dispatch(self):
        app = self.server
        addr = urlsplit(self.path)
        host = self.headers.get("Host")
        port = app.server_port
        valid_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if port == 80:
            valid_hosts.update(("127.0.0.1", "localhost"))
        if host not in valid_hosts:
            return self.send_json(403, {"error": "Use the local server address"})
        origin = self.headers.get("Origin")
        if origin and self.command == "POST" and origin != f"http://{host}":
            return self.send_json(403, {"error": "Cross-origin writes are disabled"})
        path = addr.path.rstrip("/")
        if self.command == "GET" and path in ("", "/index.html", "/site.css", "/app.js"):
            name = path.lstrip("/") or "index.html"
            return self.send_bytes(200, (contracts.HARNESS_ROOT / "web" / name).read_bytes(), STATIC[name])
        if self.command == "GET" and path == BASE:
            return self.send_json(200, {"items": [{"id": app.project}], "can_launch": bool(app.prompt)})
        route = BASE + "/" + app.project
        if self.command == "GET" and path == route:
            return self.send_json(200, projects.snapshot(app.state, app.project))
        if self.command == "GET" and path == route + "/events":
            cursor = self.headers.get("Last-Event-ID") or parse_qs(addr.query).get("after", ["0"])[0]
            return self.stream(int(cursor))
        if self.command == "POST" and path == route + "/messages":
            body = self.body()
            kind = body.get("kind", "feedback")
            if kind == "feedback":
                receipt = projects.feedback(app.state, app.project, body.get("client_message_id"), body.get("text"))
            elif kind == "launch":
                if app.prompt is None:
                    raise projects.Conflict("Enable launch by supplying --prompt-file to gx-web")
                has_driver = (app.state / "project-driver.json").is_file()
                current_prompt = None if has_driver else app.prompt
                receipt = projects.launch_project(app.state, app.project, prompt=current_prompt,
                                                  settings={} if has_driver else app.settings)
            else:
                raise ValueError("Use kind feedback or launch")
            return self.send_json(202, receipt)
        self.send_json(404, {"error": "Unknown project or route"})

    def do_GET(self):
        try:
            self.dispatch()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except projects.Conflict as exc:
            self.send_json(409, {"error": str(exc)})
        except (ValueError, GxError) as exc:
            self.send_json(400, {"error": str(exc)})
        except (OSError, sqlite3.Error, RuntimeError) as exc:
            self.send_json(503, {"error": str(exc)})

    do_POST = do_GET


def make_server(state, project, port, *, prompt=None, settings=None):
    if not re.fullmatch(r"[a-zA-Z0-9_-][a-zA-Z0-9._-]{0,127}", project):
        raise ValueError("Use a project name containing letters, digits, dots, hyphens or underscores")
    if prompt is not None and not Path(prompt).is_file():
        raise ValueError("The selected prompt file does not exist")
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.state, server.project = Path(state).resolve(), project
    server.prompt = Path(prompt).resolve() if prompt else None
    server.settings = settings or {}
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--project", required=True)
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--prompt-file", type=Path, help="Enable starting this Markdown commission from the page")
    parser.add_argument("--model")
    parser.add_argument("--collaboration", choices=("auto", "module-leads"))
    parser.add_argument("--max-sessions", type=int)
    parser.add_argument("--max-wall-seconds", type=int)
    args = parser.parse_args(argv)
    settings = {k: v for k, v in vars(args).items() if k in
                ("model", "collaboration", "max_sessions", "max_wall_seconds") and v is not None}
    try:
        with make_server(args.state_dir, args.project, args.port, prompt=args.prompt_file, settings=settings) as server:
            print(f"GX project {args.project}: http://127.0.0.1:{server.server_port}/", flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")
