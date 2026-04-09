import os
import signal
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from .session import Session


def kill_pid(pid: Optional[int]) -> None:
    if pid is None:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


def unmount_sshfs(mount_path: Optional[str]) -> None:
    if mount_path is None or not Path(mount_path).is_mount():
        return
    try:
        subprocess.run(["fusermount", "-u", mount_path], check=True,
                       capture_output=True, timeout=5)
        return
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass
    try:
        subprocess.run(["fusermount", "-uz", mount_path], check=True,
                       capture_output=True, timeout=5)
        return
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass
    # Last resort — may prompt for sudo password
    print(f"fusermount failed, trying sudo umount {mount_path}")
    subprocess.run(["sudo", "umount", "-l", mount_path], timeout=30)


def teardown(session: Session) -> None:
    kill_pid(session.zathura_pid)
    kill_pid(session.tunnel_pid)
    unmount_sshfs(session.mount_path)
    sdir = session.dir
    if sdir.exists():
        shutil.rmtree(sdir, ignore_errors=True)
    print(f"Stopped session: {session.project}")


def install_signal_handlers(session: Session) -> None:
    def handler(signum, frame):
        teardown(session)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)
