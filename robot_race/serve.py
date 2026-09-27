"""Tiny dev server over runs/ for the website team (stdlib only).

python -m robot_race.serve --port 8000 --runs runs

Static:  GET /<run_id>/attempt_<k>/attempt.mp4 ...  (HTTP Range -> 206, so <video> can seek)
API:     GET /api/runs                              -> manifest (same as runs/index.json, built live)
         GET /api/runs/<run_id>                     -> summary.json (synthesized for run_policy dirs)
         GET /api/runs/<run_id>/events?since=<n>    -> {"events": [...lines n..], "next": <n'>}
         GET /api/runs/<run_id>/transcript          -> {"turns": [transcript.jsonl lines]} (one per model call)
All responses: Access-Control-Allow-Origin: *. JSON / events / live.jpg: Cache-Control: no-store.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from . import viewer

RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
MIME = {".mp4": "video/mp4", ".jsonl": "application/x-ndjson", ".npz": "application/octet-stream",
        ".json": "application/json", ".md": "text/markdown; charset=utf-8", ".py": "text/plain; charset=utf-8",
        ".jpg": "image/jpeg", ".png": "image/png", ".gif": "image/gif", ".html": "text/html; charset=utf-8"}
NO_STORE = (".json", ".jsonl", "live.jpg", ".html")


def parse_range(header: str, size: int) -> tuple[int, int] | None:
    """First range of a 'bytes=a-b' header -> (start, end) inclusive. None = unsatisfiable/invalid.
    Raises ValueError for headers we ignore (non-bytes units)."""
    unit, _, spec = header.partition("=")
    if unit.strip().lower() != "bytes" or not spec:
        raise ValueError(header)
    first = spec.split(",")[0].strip()
    a, sep, b = first.partition("-")
    if not sep:
        return None
    a, b = a.strip(), b.strip()
    try:
        if a == "":  # suffix: last b bytes
            n = int(b)
            if n <= 0 or size == 0:
                return None
            return max(0, size - n), size - 1
        start = int(a)
        end = int(b) if b else size - 1
    except ValueError:
        return None
    if start < 0 or start >= size or end < start:
        return None
    return start, min(end, size - 1)


class Handler(SimpleHTTPRequestHandler):
    server_version = "RobotRace/1"

    def __init__(self, *args, runs_root: str, **kw):
        self.runs_root = os.path.realpath(runs_root)
        super().__init__(*args, directory=self.runs_root, **kw)

    def log_request(self, code="-", size="-"):  # quiet: the UI polls a lot; log errors only
        if str(getattr(code, "value", code)).startswith(("4", "5")):
            super().log_request(code, size)

    # -------------------------------------------------------- plumbing
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Expose-Headers", "Content-Range, Content-Length, Accept-Ranges")
        path = urlsplit(self.path).path
        if path.startswith("/api/") or path.endswith(NO_STORE) or path.endswith("/"):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Range, Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json(self, obj, status=HTTPStatus.OK, head=False):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if not head:
            self.wfile.write(data)

    def _error(self, status, msg, head=False):
        self._json({"error": msg}, status, head)

    def do_GET(self):
        self._handle(head=False)

    def do_HEAD(self):
        self._handle(head=True)

    def _handle(self, head: bool):
        u = urlsplit(self.path)
        raw = unquote(u.path)
        if "\x00" in raw or any(p == ".." for p in re.split(r"[/\\]", raw)):
            return self._error(HTTPStatus.FORBIDDEN, "forbidden", head)
        if raw == "/api/runs" or raw.startswith("/api/runs/"):
            return self._api(raw, parse_qs(u.query), head)
        if raw.startswith("/api"):
            return self._error(HTTPStatus.NOT_FOUND, "not found", head)
        self._static(raw, head)

    def _safe(self, rel: str) -> str | None:
        p = os.path.realpath(os.path.join(self.runs_root, rel.lstrip("/")))
        if p != self.runs_root and not p.startswith(self.runs_root + os.sep):
            return None
        return p

    # -------------------------------------------------------- API
    def _api(self, raw: str, q: dict, head: bool):
        parts = [p for p in raw.split("/") if p][2:]  # drop "api", "runs"
        if not parts:
            return self._json(viewer.build_manifest(self.runs_root), head=head)
        run_id = parts[0]
        run_dir = self._safe(run_id) if RUN_ID.match(run_id) else None
        if not run_dir or not os.path.isdir(run_dir):
            return self._error(HTTPStatus.NOT_FOUND, "no such run", head)
        if len(parts) == 1:
            spath = os.path.join(run_dir, "summary.json")
            if os.path.exists(spath):
                s = viewer._load_json(spath)
                if s is None:  # mid-write; client should retry
                    return self._error(HTTPStatus.SERVICE_UNAVAILABLE, "summary not readable yet", head)
                return self._json(s, head=head)
            info = viewer.run_info(run_dir)
            if not info:
                return self._error(HTTPStatus.NOT_FOUND, "no such run", head)
            attempts = [dict(dir=n, **(info["results"][n] or {})) for n in info["done"]]
            return self._json({k: info[k] for k in ("task", "seed", "model", "strategy", "status", "solved_at",
                                                    "kind", "seeds")} | {"attempts": attempts}, head=head)
        if len(parts) == 2 and parts[1] == "events":
            try:
                since = max(0, int(q.get("since", ["0"])[0]))
            except ValueError:
                return self._error(HTTPStatus.BAD_REQUEST, "since must be an int", head)
            return self._json(read_events(os.path.join(run_dir, "events.jsonl"), since), head=head)
        if len(parts) == 2 and parts[1] == "transcript":
            turns = read_events(os.path.join(run_dir, "transcript.jsonl"))["events"]
            return self._json({"turns": turns}, head=head)
        return self._error(HTTPStatus.NOT_FOUND, "not found", head)

    # -------------------------------------------------------- static files with Range
    def _static(self, raw: str, head: bool):
        path = self._safe(raw)
        if path is None:
            return self._error(HTTPStatus.FORBIDDEN, "forbidden", head)
        if os.path.isdir(path):
            if not raw.endswith("/"):
                self.send_response(HTTPStatus.MOVED_PERMANENTLY)
                self.send_header("Location", raw + "/")
                self.send_header("Content-Length", "0")
                return self.end_headers()
            idx = os.path.join(path, "index.html")
            if not os.path.isfile(idx):
                return self._error(HTTPStatus.NOT_FOUND, "not found", head)
            path = idx
        try:
            f = open(path, "rb")
        except OSError:
            return self._error(HTTPStatus.NOT_FOUND, "not found", head)
        with f:
            size = os.fstat(f.fileno()).st_size
            ctype = MIME.get(os.path.splitext(path)[1].lower()) or mimetypes.guess_type(path)[0] \
                or "application/octet-stream"
            start, end, status = 0, size - 1, HTTPStatus.OK
            rng = self.headers.get("Range")
            if rng:
                try:
                    r = parse_range(rng, size)
                except ValueError:  # not a bytes range: ignore it, send the whole file
                    r, rng = None, None
                if rng and r is None:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    return self.end_headers()
                if rng:
                    start, end, status = r[0], r[1], HTTPStatus.PARTIAL_CONTENT
            length = max(0, end - start + 1)
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Last-Modified", self.date_time_string(int(os.fstat(f.fileno()).st_mtime)))
            if status == HTTPStatus.PARTIAL_CONTENT:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if head or length == 0:
                return
            f.seek(start)
            left = length
            try:
                while left > 0:
                    chunk = f.read(min(64 * 1024, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):  # browsers abort video requests all the time
                pass


def read_events(path: str, since: int = 0) -> dict:
    """Complete lines of events.jsonl from line `since` (0-based). A trailing line without a newline is
    still being written: it's left for the next poll. Unparseable complete lines are skipped (but counted)."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return {"events": [], "next": since}
    lines = data.split(b"\n")[:-1]  # last element is "" or a partial line
    events = []
    for ln in lines[since:]:
        try:
            if ln.strip():
                events.append(json.loads(ln))
        except ValueError:
            pass
    return {"events": events, "next": max(since, len(lines))}


def make_server(runs_root: str = "runs", host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    os.makedirs(runs_root, exist_ok=True)
    srv = ThreadingHTTPServer((host, port), partial(Handler, runs_root=runs_root))
    srv.daemon_threads = True
    return srv


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Serve runs/ with Range, CORS and a small JSON API.")
    ap.add_argument("--runs", default="runs")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args(argv)
    srv = make_server(a.runs, a.host, a.port)
    print(f"serving {os.path.abspath(a.runs)} at http://{a.host}:{srv.server_address[1]}/  (api: /api/runs)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
