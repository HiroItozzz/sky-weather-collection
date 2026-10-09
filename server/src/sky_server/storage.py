"""保存先のインターフェースと、ローカル（ファイル）版の実装。"""

import json
import os
import shutil
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Iterator
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

    @abstractmethod
    def get_daily_count(self, user_id: str, day: str) -> int:
        """撮影者のその日（`YYYYMMDD`）の新規の観測の件数。記録がなければ 0。"""

    @abstractmethod
    def increment_daily_count(self, user_id: str, day: str) -> None:
        """撮影者のその日（`YYYYMMDD`）の件数を 1 増やす。"""

    @abstractmethod
    def list_observations_by_user(self, user_id: str) -> list[dict]:
        """撮影者の観測をすべて返す。"""

    @abstractmethod
    def update_observation_fields(self, observation_id: str, fields: dict) -> None:
        """観測の項目を、渡したものだけ書き換える（なければ足す）。観測がなければ例外を投げる。"""

    @abstractmethod
    def list_observations_page(
        self, user_id: str, limit: int, before: tuple[str, str] | None
    ) -> list[dict]:
        """撮影者の観測を `(captured_at_utc, observation_id)` の降順で最大 `limit` 件返す。

        `before` があれば、その組より後ろ（並び順で）のものだけを返す。
        `captured_at_utc` がない観測は含めない。
        """

    @abstractmethod
    def list_all_observations(self) -> Iterator[dict]:
        """すべての観測を返す（撮影者を問わない）。"""

    @abstractmethod
    def delete_observation(self, observation_id: str) -> None:
        """観測を消す。なければ何もしない。"""

    @abstractmethod
    def delete_daily_counts(self, user_id: str) -> None:
        """撮影者の送信数の記録（`usage`）をすべて消す。なければ何もしない。"""

    @abstractmethod
    def delete_user(self, user_id: str) -> None:
        """撮影者を消す。なければ何もしない。"""


class BlobStore(ABC):
    """画像や天気データ（バイト列）の保存先。"""

    @abstractmethod
    def put(self, key: str, data: bytes) -> None: ...

    @abstractmethod
    def get(self, key: str) -> bytes | None: ...

    @abstractmethod
    def delete(self, key: str) -> None:
        """消す。なければ何もしない。"""

    @abstractmethod
    def delete_prefix(self, prefix: str) -> None:
        """接頭辞の下にあるものをすべて消す。なければ何もしない。

        `prefix` は `/` で終わる形で渡す（`ab` で `abc/` まで消えないように）。
        """


class LocalObservationRepository(ObservationRepository):
    """1件1ファイルの JSON で保存する。"""

    def __init__(self, root: Path) -> None:
        self._users = root / "users"
        self._observations = root / "observations"
        self._usage = root / "usage"
        self._users.mkdir(parents=True, exist_ok=True)
        self._observations.mkdir(parents=True, exist_ok=True)
        self._usage.mkdir(parents=True, exist_ok=True)

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
        data = json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8")
        return _create_exclusive(path, data)

    def get_daily_count(self, user_id: str, day: str) -> int:
        path = self._usage / f"{user_id}_{day}.json"
        if not path.exists():
            return 0
        return json.loads(path.read_text(encoding="utf-8"))["count"]

    def increment_daily_count(self, user_id: str, day: str) -> None:
        # 読んでから書くまでの間の同時実行で数え漏らすことは許容する（上限は被害を抑えるためのもの）
        count = self.get_daily_count(user_id, day) + 1
        data = {"user_id": user_id, "day": day, "count": count}
        _write_atomic(
            self._usage / f"{user_id}_{day}.json",
            json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
        )

    def list_observations_by_user(self, user_id: str) -> list[dict]:
        records = (
            json.loads(path.read_text(encoding="utf-8"))
            for path in self._observations.glob("*.json")
        )
        return [record for record in records if record.get("user_id") == user_id]

    def update_observation_fields(self, observation_id: str, fields: dict) -> None:
        path = self._observations / f"{observation_id}.json"
        record = {**json.loads(path.read_text(encoding="utf-8")), **fields}
        _write_atomic(path, json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8"))

    def list_observations_page(
        self, user_id: str, limit: int, before: tuple[str, str] | None
    ) -> list[dict]:
        records = [
            record
            for record in self.list_observations_by_user(user_id)
            if "captured_at_utc" in record
        ]
        records.sort(key=lambda r: (r["captured_at_utc"], r["observation_id"]), reverse=True)
        if before is not None:
            records = [r for r in records if (r["captured_at_utc"], r["observation_id"]) < before]
        return records[:limit]

    def list_all_observations(self) -> Iterator[dict]:
        # 呼び出し側が書き換えながら読んでも問題ないように、先にファイルの一覧を固める
        for path in sorted(self._observations.glob("*.json")):
            if path.exists():
                yield json.loads(path.read_text(encoding="utf-8"))

    def delete_observation(self, observation_id: str) -> None:
        (self._observations / f"{observation_id}.json").unlink(missing_ok=True)

    def delete_daily_counts(self, user_id: str) -> None:
        for path in self._usage.glob("*.json"):
            if json.loads(path.read_text(encoding="utf-8")).get("user_id") == user_id:
                path.unlink(missing_ok=True)

    def delete_user(self, user_id: str) -> None:
        (self._users / f"{user_id}.json").unlink(missing_ok=True)


class LocalBlobStore(BlobStore):
    def __init__(self, root: Path, dirname: str = "images") -> None:
        self._root = root / dirname
        self._root.mkdir(parents=True, exist_ok=True)

    def put(self, key: str, data: bytes) -> None:
        path = self._root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(path, data)

    def get(self, key: str) -> bytes | None:
        path = self._root / key
        return path.read_bytes() if path.exists() else None

    def delete(self, key: str) -> None:
        (self._root / key).unlink(missing_ok=True)

    def delete_prefix(self, prefix: str) -> None:
        check_prefix(prefix)
        directory = (self._root / prefix).resolve()
        if not directory.is_relative_to(self._root.resolve()) or directory == self._root.resolve():
            raise ValueError(f"保存先の外や全体は消せません: {prefix!r}")
        shutil.rmtree(directory, ignore_errors=True)


def check_prefix(prefix: str) -> None:
    """接頭辞が `/` で終わり、空でないことを確かめる。違えば ValueError。"""
    if not prefix.endswith("/") or prefix.strip("/") == "":
        raise ValueError(f"接頭辞は空でない、/ で終わる形にしてください: {prefix!r}")


def _create_exclusive(path: Path, data: bytes) -> bool:
    """ファイルがなければ `data` で作って True、すでにあれば何もせず False を返す。

    一時ファイルに書き終えてからハードリンクで置くので、書きかけのファイルが見えることはない。
    同時に同じパスを作ろうとしても、リンクに成功するのは片方だけ。
    """
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        try:
            os.link(tmp, path)
        except FileExistsError:
            return False
        return True
    finally:
        os.unlink(tmp)


def _write_atomic(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
