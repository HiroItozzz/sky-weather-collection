"""保存先のインターフェースと、ローカル（ファイル）版の実装。"""

import json
import os
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path

from sky_server.models import User


class ObservationRepository(ABC):
    """撮影者と観測（メタデータ）の保存先。"""

    @abstractmethod
    def add_user(self, user: User) -> None: ...

    @abstractmethod
    def get_user(self, user_id: str) -> User | None: ...

    @abstractmethod
    def get_user_by_token_hash(self, token_hash: str) -> User | None: ...

    @abstractmethod
    def update_user(self, user: User) -> None: ...

    @abstractmethod
    def get_observation(self, observation_id: str) -> dict | None: ...

    @abstractmethod
    def add_observation(self, record: dict) -> bool:
        """観測を保存する。同じ ID がすでにあれば保存せず False を返す。"""


class BlobStore(ABC):
    """画像の保存先。"""

    @abstractmethod
    def put(self, key: str, data: bytes) -> None: ...

    @abstractmethod
    def get(self, key: str) -> bytes | None: ...


class LocalObservationRepository(ObservationRepository):
    """1件1ファイルの JSON で保存する。"""

    def __init__(self, root: Path) -> None:
        self._users = root / "users"
        self._observations = root / "observations"
        self._users.mkdir(parents=True, exist_ok=True)
        self._observations.mkdir(parents=True, exist_ok=True)

    def add_user(self, user: User) -> None:
        self.update_user(user)

    def get_user(self, user_id: str) -> User | None:
        path = self._users / f"{user_id}.json"
        if not path.exists():
            return None
        return User.model_validate_json(path.read_text(encoding="utf-8"))

    def get_user_by_token_hash(self, token_hash: str) -> User | None:
        for path in self._users.glob("*.json"):
            user = User.model_validate_json(path.read_text(encoding="utf-8"))
            if user.token_hash == token_hash:
                return user
        return None

    def update_user(self, user: User) -> None:
        _write_atomic(self._users / f"{user.user_id}.json", user.model_dump_json(indent=2).encode())

    def get_observation(self, observation_id: str) -> dict | None:
        path = self._observations / f"{observation_id}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def add_observation(self, record: dict) -> bool:
        path = self._observations / f"{record['observation_id']}.json"
        try:
            # 排他的に作成して、同時に同じ ID が来たときに片方だけが成功するようにする
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
        return True


class LocalBlobStore(BlobStore):
    def __init__(self, root: Path) -> None:
        self._root = root / "images"
        self._root.mkdir(parents=True, exist_ok=True)

    def put(self, key: str, data: bytes) -> None:
        path = self._root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(path, data)

    def get(self, key: str) -> bytes | None:
        path = self._root / key
        return path.read_bytes() if path.exists() else None


def _write_atomic(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
