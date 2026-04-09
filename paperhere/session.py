import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from .config import session_dir


@dataclass
class Session:
    project: str
    mode: str  # "local" or "remote"
    zathura_pid: Optional[int] = None
    tunnel_pid: Optional[int] = None
    listener_port: Optional[int] = None
    mount_path: Optional[str] = None
    server: Optional[str] = None
    remote_dir: Optional[str] = None
    project_dir: Optional[str] = None

    @property
    def dir(self) -> Path:
        return session_dir(self.project)

    @property
    def state_file(self) -> Path:
        return self.dir / "session.json"

    def save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(asdict(self), indent=2))

    @classmethod
    def load(cls, path: Path) -> "Session":
        data = json.loads(path.read_text())
        return cls(**data)

    @classmethod
    def find_all(cls) -> list["Session"]:
        sessions = []
        for p in Path("/tmp").glob("paperhere-*/session.json"):
            try:
                sessions.append(cls.load(p))
            except (json.JSONDecodeError, TypeError, KeyError):
                continue
        return sessions

    @classmethod
    def find(cls, project: str) -> Optional["Session"]:
        state_file = session_dir(project) / "session.json"
        if state_file.exists():
            return cls.load(state_file)
        return None
