import socket
import subprocess
import threading
from typing import Optional


class ForwardSearchListener:
    """TCP listener that receives forward search commands from remote nvim
    and dispatches them to the local zathura instance."""

    def __init__(self, port: int, zathura_pid: int,
                 remote_dir: str, local_mount: str):
        self.port = port
        self.zathura_pid = zathura_pid
        self.remote_dir = remote_dir
        self.local_mount = local_mount
        self._server: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self) -> None:
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", self.port))
        self._server.listen(4)
        self._server.settimeout(1.0)  # allow periodic stop checks
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._server:
            self._server.close()
        if self._thread:
            self._thread.join(timeout=3)

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                try:
                    data = conn.recv(4096).decode().strip()
                except Exception:
                    continue
            if data.startswith("FORWARD "):
                self._handle_forward(data[8:])

    def _handle_forward(self, payload: str) -> None:
        # payload: <line>:<col>:<remote-tex-path> <remote-pdf-path>
        parts = payload.split(" ", 1)
        if len(parts) != 2:
            return

        synctex_spec = parts[0]  # line:col:remote-tex-path
        remote_pdf = parts[1]

        # Split synctex_spec into line:col:path
        spec_parts = synctex_spec.split(":", 2)
        if len(spec_parts) != 3:
            return
        line, col, remote_tex = spec_parts

        # Translate remote paths to local mount paths
        local_tex = remote_tex.replace(self.remote_dir, self.local_mount, 1)
        local_pdf = remote_pdf.replace(self.remote_dir, self.local_mount, 1)

        # Call zathura for forward search
        subprocess.Popen(
            [
                "zathura", "--synctex-forward",
                f"{line}:{col}:{local_tex}",
                "--synctex-pid", str(self.zathura_pid),
                local_pdf,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def check_port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def start_reverse_tunnel(server: str, port: int) -> subprocess.Popen:
    return subprocess.Popen(
        ["ssh", "-R", f"{port}:localhost:{port}", server, "-N"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
