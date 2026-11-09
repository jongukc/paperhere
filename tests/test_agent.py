from __future__ import annotations

import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import paperhere.agent as agent_module
from paperhere.agent import AgentState, PaperhereServer, find_main_tex
from paperhere.synctex import PdfPosition, SourcePosition


class AgentStateTests(unittest.TestCase):
    def test_discovers_document_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "notes.tex").write_text("some notes")
            expected = root / "main.tex"
            expected.write_text("\\documentclass{article}\n")
            self.assertEqual(find_main_tex(root), expected.resolve())

    def test_paths_cannot_escape_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = AgentState(
                root=root,
                token="secret",
                pdf=None,
                nvim_socket=None,
                nvim_executable="nvim",
            )
            with self.assertRaisesRegex(ValueError, "outside the project"):
                state.inside_root(Path("../elsewhere.pdf"))

    def test_forward_broadcasts_synctex_location(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tex = root / "p.tex"
            pdf = root / "p.pdf"
            tex.write_text("\\documentclass{article}\n")
            pdf.write_bytes(b"%PDF-test")
            state = AgentState(
                root=root,
                token="secret",
                pdf=pdf,
                nvim_socket=None,
                nvim_executable="nvim",
            )
            subscriber = state.subscribe()
            position = PdfPosition(2, 10, 20, 11, 21, 30, 8)
            with patch("paperhere.agent.synctex.forward", return_value=position):
                result = state.forward(
                    {"tex": str(tex), "pdf": str(pdf), "line": 9, "column": 2}
                )

            self.assertEqual(result["type"], "forward")
            self.assertEqual(result["page"], 2)
            self.assertEqual(subscriber.get_nowait(), result)

    def test_inverse_sends_location_to_attached_nvim(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tex = root / "p.tex"
            pdf = root / "p.pdf"
            tex.write_text("\\documentclass{article}\n")
            pdf.write_bytes(b"%PDF-test")
            state = AgentState(
                root=root,
                token="secret",
                pdf=pdf,
                nvim_socket="/tmp/nvim.sock",
                nvim_executable="/usr/bin/nvim",
            )
            completed = __import__("subprocess").CompletedProcess([], 0, "", "")
            with (
                patch(
                    "paperhere.agent.synctex.inverse",
                    return_value=SourcePosition(tex, 12, 3),
                ),
                patch("paperhere.agent.subprocess.run", return_value=completed) as run,
            ):
                result = state.inverse({"page": 1, "x": 20, "y": 30})

            self.assertEqual(result, {"path": str(tex), "line": 12, "column": 3})
            command = run.call_args.args[0]
            self.assertEqual(command[:3], ["/usr/bin/nvim", "--server", "/tmp/nvim.sock"])
            self.assertIn("PaperhereInverse", command[-1])

    def test_broadcast_tolerates_stalled_subscribers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = AgentState(
                root=Path(temporary),
                token="secret",
                pdf=None,
                nvim_socket=None,
                nvim_executable="nvim",
            )
            subscriber = state.subscribe()
            for index in range(40):
                state.broadcast({"type": "reload", "revision": index})
            # A listener that stopped reading keeps only the latest events.
            self.assertEqual(subscriber.qsize(), 32)
            self.assertEqual(subscriber.get_nowait()["revision"], 8)

            errors: list[BaseException] = []

            def flood(offset: int) -> None:
                try:
                    for index in range(2000):
                        state.broadcast({"type": "reload", "revision": offset + index})
                except BaseException as exc:  # noqa: BLE001 - recorded for the assertion
                    errors.append(exc)

            threads = [threading.Thread(target=flood, args=(n * 10000,)) for n in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            # Concurrent broadcasts into a full queue must never raise.
            self.assertEqual(errors, [])
            self.assertEqual(subscriber.qsize(), 32)


class AgentHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.pdf = self.root / "p.pdf"
        self.pdf.write_bytes(b"0123456789")
        state = AgentState(
            root=self.root,
            token="secret",
            pdf=self.pdf,
            nvim_socket=None,
            nvim_executable="nvim",
        )
        self.server = PaperhereServer(("127.0.0.1", 0), state)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def test_status_requires_session_token(self) -> None:
        with self.assertRaises(HTTPError) as context:
            urlopen(f"{self.base}/api/status", timeout=2)
        self.assertEqual(context.exception.code, 403)

        with urlopen(f"{self.base}/api/status?token=secret", timeout=2) as response:
            payload = json.load(response)
        self.assertTrue(payload["pdfReady"])

    def test_pdf_supports_explicit_and_suffix_ranges(self) -> None:
        request = Request(
            f"{self.base}/document.pdf?token=secret", headers={"Range": "bytes=2-5"}
        )
        with urlopen(request, timeout=2) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), b"2345")
            self.assertEqual(response.headers["Content-Range"], "bytes 2-5/10")

        request = Request(
            f"{self.base}/document.pdf?token=secret", headers={"Range": "bytes=-3"}
        )
        with urlopen(request, timeout=2) as response:
            self.assertEqual(response.read(), b"789")

        request = Request(
            f"{self.base}/document.pdf?token=secret", headers={"Range": "bytes=20-30"}
        )
        with self.assertRaises(HTTPError) as context:
            urlopen(request, timeout=2)
        self.assertEqual(context.exception.code, 416)
        self.assertEqual(context.exception.headers["Content-Range"], "bytes */10")

    def test_static_viewer_is_served(self) -> None:
        with urlopen(f"{self.base}/", timeout=2) as response:
            body = response.read().decode()
        self.assertIn("Paperhere", body)
        self.assertIn("Keyboard navigation", body)

    def test_static_assets_revalidate_with_etag(self) -> None:
        with urlopen(f"{self.base}/static/app.js", timeout=2) as response:
            etag = response.headers["ETag"]
            self.assertEqual(response.headers["Cache-Control"], "no-cache")
        self.assertTrue(etag)
        request = Request(f"{self.base}/static/app.js", headers={"If-None-Match": etag})
        with self.assertRaises(HTTPError) as context:
            urlopen(request, timeout=2)
        self.assertEqual(context.exception.code, 304)

    def _open_raw(self, request_line: str) -> socket.socket:
        sock = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2)
        sock.sendall(f"{request_line} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
        return sock

    @staticmethod
    def _read_until(sock: socket.socket, needle: bytes, timeout: float = 3) -> bytes:
        deadline = time.monotonic() + timeout
        buffer = b""
        while needle not in buffer and time.monotonic() < deadline:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                break
            buffer += chunk
        return buffer

    def test_event_stream_sends_retry_status_and_pings(self) -> None:
        with patch.object(agent_module, "EVENT_PING_INTERVAL", 0.05):
            sock = self._open_raw("GET /events?token=secret")
            try:
                data = self._read_until(sock, b'data: {"type":"ping"}\n\n')
            finally:
                sock.close()
            self.assertIn(b"Content-Type: text/event-stream", data)
            self.assertIn(b"retry: 1000\n\n", data)
            self.assertIn(b'"type":"status"', data)
            self.assertIn(b'"pdfReady":true', data)
            self.assertIn(b'data: {"type":"ping"}\n\n', data)
            # The next write hits the closed socket and the subscriber is dropped.
            deadline = time.monotonic() + 3
            while self.server.state._subscribers and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(len(self.server.state._subscribers), 0)

    def test_document_download_survives_client_disconnect(self) -> None:
        self.pdf.write_bytes(b"%" * (8 * 1024 * 1024))
        failures: list[object] = []
        with patch.object(self.server, "handle_error", lambda *args: failures.append(args)):
            sock = self._open_raw("GET /document.pdf?token=secret")
            self._read_until(sock, b"\r\n\r\n")
            sock.close()
            time.sleep(0.3)
        self.assertEqual(failures, [])

    def test_missing_forward_files_return_json_error(self) -> None:
        payload = json.dumps(
            {
                "type": "view",
                "tex": str(self.root / "missing.tex"),
                "pdf": str(self.pdf),
                "line": 1,
                "column": 1,
            }
        ).encode()
        request = Request(
            f"{self.base}/api/event?token=secret",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as context:
            urlopen(request, timeout=2)
        self.assertEqual(context.exception.code, 400)
        error = json.load(context.exception)
        self.assertIn("missing.tex", error["error"])


if __name__ == "__main__":
    unittest.main()
