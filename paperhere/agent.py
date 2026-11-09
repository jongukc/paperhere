from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import synctex


MAX_REQUEST_SIZE = 1024 * 1024
# Firefox closes connections that stay silent for 5 s after a network change
# notification, and its EventSource treats that as fatal. Keep the stream
# chatty enough to survive it.
EVENT_PING_INTERVAL = 4.0
# A browser that vanished without closing its socket must not pin a handler
# thread, and its subscriber queue, forever.
EVENT_WRITE_TIMEOUT = 30.0


def find_main_tex(root: Path) -> Path | None:
    preferred = ("main.tex", "paper.tex", f"{root.name}.tex", "p.tex")
    candidates = sorted(root.glob("*.tex"))
    roots: list[Path] = []
    for candidate in candidates:
        try:
            prefix = candidate.read_text(errors="ignore")[:65536]
        except OSError:
            continue
        if "\\documentclass" in prefix:
            roots.append(candidate)
    if len(roots) == 1:
        return roots[0].resolve()
    by_name = {candidate.name: candidate for candidate in roots}
    for name in preferred:
        if name in by_name:
            return by_name[name].resolve()
    return roots[0].resolve() if roots else None


class AgentState:
    def __init__(
        self,
        *,
        root: Path,
        token: str,
        pdf: Path | None,
        nvim_socket: str | None,
        nvim_executable: str,
    ) -> None:
        self.root = root.resolve()
        self.token = token
        self.nvim_socket = nvim_socket
        self.nvim_executable = nvim_executable
        self.pdf: Path | None = None
        self.revision = 0
        self._signature: tuple[int, int] | None = None
        self._lock = threading.RLock()
        self._subscribers: set[queue.Queue[dict[str, Any]]] = set()
        if pdf is not None:
            self.set_pdf(pdf, notify=False)

    def inside_root(self, path: Path, *, must_exist: bool = False) -> Path:
        candidate = path if path.is_absolute() else self.root / path
        resolved = candidate.resolve(strict=must_exist)
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(f"Path is outside the project: {resolved}") from exc
        return resolved

    @staticmethod
    def _file_signature(path: Path) -> tuple[int, int] | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        return stat.st_mtime_ns, stat.st_size

    def set_pdf(self, path: Path, *, notify: bool = True) -> bool:
        resolved = self.inside_root(path)
        if resolved.suffix.lower() != ".pdf":
            raise ValueError("The preview target must be a PDF")
        signature = self._file_signature(resolved)
        with self._lock:
            changed = resolved != self.pdf or signature != self._signature
            self.pdf = resolved
            self._signature = signature
            if notify and changed and signature is not None:
                self.revision += 1
                event = {"type": "reload", "revision": self.revision}
            else:
                event = None
        if event is not None:
            self.broadcast(event)
        return changed

    def refresh_if_changed(self) -> bool:
        with self._lock:
            pdf = self.pdf
            previous = self._signature
        if pdf is None:
            return False
        signature = self._file_signature(pdf)
        if signature is None or signature == previous:
            return False
        time.sleep(0.2)
        if self._file_signature(pdf) != signature:
            return False
        with self._lock:
            if signature == self._signature:
                return False
            self._signature = signature
            self.revision += 1
            revision = self.revision
        self.broadcast({"type": "reload", "revision": revision})
        return True

    def status(self) -> dict[str, Any]:
        with self._lock:
            pdf = self.pdf
            return {
                "root": str(self.root),
                "pdf": str(pdf) if pdf else None,
                "pdfReady": bool(pdf and pdf.is_file()),
                "revision": self.revision,
            }

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        subscriber: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=32)
        with self._lock:
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    def broadcast(self, event: dict[str, Any]) -> None:
        with self._lock:
            subscribers = tuple(self._subscribers)
        for subscriber in subscribers:
            while True:
                try:
                    subscriber.put_nowait(event)
                    break
                except queue.Full:
                    # A stalled listener only needs the latest events. Retry
                    # after dropping one so a concurrent broadcast cannot
                    # raise out of here.
                    try:
                        subscriber.get_nowait()
                    except queue.Empty:
                        pass

    def forward(self, payload: dict[str, Any]) -> dict[str, Any]:
        pdf = self.inside_root(Path(str(payload["pdf"])), must_exist=True)
        source = self.inside_root(Path(str(payload["tex"])), must_exist=True)
        self.set_pdf(pdf)
        position = synctex.forward(
            source=source,
            line=max(1, int(payload["line"])),
            column=max(1, int(payload.get("column", 1))),
            pdf=pdf,
            cwd=self.root,
        )
        event = {"type": "forward", **asdict(position)}
        self.broadcast(event)
        return event

    def inverse(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            pdf = self.pdf
        if pdf is None or not pdf.is_file():
            raise ValueError("No PDF is ready")
        position = synctex.inverse(
            pdf=pdf,
            page=max(1, int(payload["page"])),
            x=float(payload["x"]),
            y=float(payload["y"]),
            cwd=self.root,
        )
        source = self.inside_root(position.path, must_exist=True)
        if not self.nvim_socket:
            raise RuntimeError("Neovim is not attached to this preview session")
        command_payload = json.dumps(
            {
                "path": str(source),
                "line": position.line,
                "column": position.column,
            },
            separators=(",", ":"),
        ).encode().hex()
        try:
            result = subprocess.run(
                [
                    self.nvim_executable,
                    "--server",
                    self.nvim_socket,
                    "--remote-send",
                    f"<Cmd>PaperhereInverse {command_payload}<CR>",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"Unable to contact Neovim: {exc}") from exc
        if result.returncode != 0:
            detail = result.stderr.strip() or "Neovim rejected the inverse search"
            raise RuntimeError(detail)
        return {
            "path": str(source),
            "line": position.line,
            "column": position.column,
        }


class PaperhereServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    # PDF.js opens several range requests at once; do not drop their SYNs.
    request_queue_size = 64

    def __init__(self, address: tuple[str, int], state: AgentState):
        self.state = state
        super().__init__(address, PaperhereHandler)


class PaperhereHandler(BaseHTTPRequestHandler):
    server: PaperhereServer

    def log_message(self, format: str, *args: object) -> None:
        if os.environ.get("PAPERHERE_DEBUG"):
            print(f"paperhere-agent: {format % args}", file=sys.stderr)

    def _parsed(self):
        return urlparse(self.path)

    def _authorized(self) -> bool:
        parsed = self._parsed()
        query_token = parse_qs(parsed.query).get("token", [""])[0]
        header = self.headers.get("Authorization", "")
        bearer = header[7:] if header.startswith("Bearer ") else ""
        return query_token == self.server.state.token or bearer == self.server.state.token

    def _require_authorized(self) -> bool:
        if self._authorized():
            return True
        self.send_error(HTTPStatus.FORBIDDEN, "Invalid session token")
        return False

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Invalid Content-Length") from exc
        if length <= 0 or length > MAX_REQUEST_SIZE:
            raise ValueError("Invalid request size")
        try:
            value = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Invalid JSON request") from exc
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object")
        return value

    def do_GET(self) -> None:
        parsed = self._parsed()
        if parsed.path == "/":
            self._serve_static("index.html")
        elif parsed.path.startswith("/static/"):
            self._serve_static(parsed.path.removeprefix("/static/"))
        elif parsed.path == "/api/status":
            if self._require_authorized():
                self._json(HTTPStatus.OK, self.server.state.status())
        elif parsed.path == "/events":
            if self._require_authorized():
                self._events()
        elif parsed.path == "/document.pdf":
            if self._require_authorized():
                self._document()
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if not self._require_authorized():
            return
        parsed = self._parsed()
        try:
            payload = self._read_json()
            if parsed.path == "/api/event":
                event_type = payload.get("type")
                if event_type == "build":
                    pdf = self.server.state.inside_root(Path(str(payload["pdf"])))
                    self.server.state.set_pdf(pdf)
                    result = self.server.state.status()
                elif event_type == "view":
                    result = self.server.state.forward(payload)
                else:
                    raise ValueError(f"Unknown event type: {event_type}")
            elif parsed.path == "/api/inverse":
                result = self.server.state.inverse(payload)
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
        except (KeyError, OSError, TypeError, ValueError, RuntimeError, synctex.SyncTexError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        self._json(HTTPStatus.OK, result)

    def _serve_static(self, relative: str) -> None:
        if not relative or ".." in Path(relative).parts:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        resource = files("paperhere").joinpath("static", relative)
        if not resource.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        data = resource.read_bytes()
        # Revalidate on every load so a viewer update is picked up by the next
        # page refresh instead of an hour later, while still skipping the body
        # for unchanged files.
        etag = f'"{hashlib.sha1(data).hexdigest()}"'
        offered = self.headers.get("If-None-Match", "")
        if etag in [value.strip() for value in offered.split(",")]:
            self.send_response(HTTPStatus.NOT_MODIFIED)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return
        content_type = mimetypes.guess_type(relative)[0] or "application/octet-stream"
        if relative.endswith(".mjs"):
            content_type = "text/javascript"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _document(self) -> None:
        status = self.server.state.status()
        pdf_value = status["pdf"]
        if not status["pdfReady"] or not pdf_value:
            self.send_error(HTTPStatus.NOT_FOUND, "PDF is not ready")
            return
        pdf = Path(pdf_value)
        try:
            stream = pdf.open("rb")
            size = os.fstat(stream.fileno()).st_size
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND, "PDF is not readable")
            return
        with stream:
            start, end = 0, size - 1
            response_status = HTTPStatus.OK
            range_header = self.headers.get("Range")
            if range_header and range_header.startswith("bytes="):
                try:
                    raw_start, raw_end = range_header[6:].split("-", 1)
                    if "," in raw_end or (not raw_start and not raw_end):
                        raise ValueError
                    if not raw_start:
                        suffix_length = int(raw_end)
                        if suffix_length <= 0:
                            raise ValueError
                        start = max(0, size - suffix_length)
                        end = size - 1
                    else:
                        start = int(raw_start)
                        end = int(raw_end) if raw_end else size - 1
                    if start < 0 or end < start or end >= size:
                        raise ValueError
                except ValueError:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                response_status = HTTPStatus.PARTIAL_CONTENT
            length = end - start + 1
            try:
                self.send_response(response_status)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Length", str(length))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "no-store")
                if response_status == HTTPStatus.PARTIAL_CONTENT:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.end_headers()
                stream.seek(start)
                remaining = length
                while remaining:
                    chunk = stream.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except OSError:
                # The browser abandoned the download (reload storm, aborted
                # range request); there is nobody left to answer.
                pass

    def _events(self) -> None:
        subscriber = self.server.state.subscribe()
        self.connection.settimeout(EVENT_WRITE_TIMEOUT)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            # Ask the browser to come back quickly after a dropped connection.
            self.wfile.write(b"retry: 1000\n\n")
            initial = {"type": "status", **self.server.state.status()}
            self._write_event(initial)
            while True:
                try:
                    event = subscriber.get(timeout=EVENT_PING_INTERVAL)
                except queue.Empty:
                    # A real event rather than an SSE comment, so the viewer
                    # can tell a quiet connection from a dead one.
                    event = {"type": "ping"}
                self._write_event(event)
        except OSError:
            # Broken pipe, reset, or a peer that stopped reading.
            pass
        finally:
            self.server.state.unsubscribe(subscriber)

    def _write_event(self, event: dict[str, Any]) -> None:
        data = json.dumps(event, separators=(",", ":")).encode()
        self.wfile.write(b"data: " + data + b"\n\n")
        self.wfile.flush()


def _watch_pdf(state: AgentState, stopped: threading.Event) -> None:
    while not stopped.wait(0.5):
        try:
            state.refresh_if_changed()
        except Exception as exc:  # noqa: BLE001 - the watcher must outlive surprises
            print(f"paperhere-agent: PDF watcher error: {exc}", file=sys.stderr)


def serve(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        print(f"Not a project directory: {root}", file=sys.stderr)
        return 2
    state = AgentState(
        root=root,
        token=args.token,
        pdf=Path(args.pdf).expanduser() if args.pdf else None,
        nvim_socket=args.nvim_socket,
        nvim_executable=args.nvim,
    )
    server = PaperhereServer((args.host, args.port), state)
    stopped = threading.Event()
    watcher = threading.Thread(target=_watch_pdf, args=(state, stopped), daemon=True)
    watcher.start()

    def stop(_signum: int, _frame: object) -> None:
        stopped.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    ready = {"host": args.host, "port": server.server_port, "pid": os.getpid()}
    print(f"PAPERHERE_READY {json.dumps(ready, separators=(',', ':'))}", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        stopped.set()
        server.server_close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="paperhere-agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser("serve")
    serve_parser.add_argument("--root", required=True)
    serve_parser.add_argument("--token", required=True)
    serve_parser.add_argument("--pdf")
    serve_parser.add_argument("--nvim-socket")
    serve_parser.add_argument("--nvim", default="nvim")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=0)

    discover_parser = subparsers.add_parser("discover")
    discover_parser.add_argument("--root", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "discover":
        root = Path(args.root).expanduser().resolve()
        main_tex = find_main_tex(root)
        print(json.dumps({"root": str(root), "tex": str(main_tex) if main_tex else None}))
        return 0 if root.is_dir() else 2
    return serve(args)


if __name__ == "__main__":
    raise SystemExit(main())
