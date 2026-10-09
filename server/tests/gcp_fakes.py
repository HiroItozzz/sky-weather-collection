"""GCP のクライアントの偽物。コードが使う範囲だけを真似る。"""

from google.api_core.exceptions import AlreadyExists, NotFound
from google.cloud import firestore


class FakeSnapshot:
    def __init__(self, data: dict | None, reference: "FakeDocument | None" = None) -> None:
        self.exists = data is not None
        self._data = data
        self.reference = reference

    def to_dict(self) -> dict | None:
        return None if self._data is None else dict(self._data)


class FakeDocument:
    def __init__(self, docs: dict, doc_id: str) -> None:
        self._docs = docs
        self._id = doc_id

    def create(self, data: dict) -> None:
        if self._id in self._docs:
            raise AlreadyExists("すでにあります")
        self._docs[self._id] = dict(data)

    def set(self, data: dict, merge: bool = False) -> None:
        if not merge:
            self._docs[self._id] = dict(data)
            return
        # merge=True では、渡した項目だけを書き換える。Increment は今の値に足す
        doc = self._docs.setdefault(self._id, {})
        for key, value in data.items():
            if isinstance(value, firestore.Increment):
                doc[key] = doc.get(key, 0) + value.value
            else:
                doc[key] = value

    def get(self) -> FakeSnapshot:
        return FakeSnapshot(self._docs.get(self._id))

    def delete(self) -> None:
        # 本物の Firestore と同じく、ないドキュメントを消してもエラーにならない
        self._docs.pop(self._id, None)


class FakeQuery:
    def __init__(self, docs: dict, field_filter=None, limit: int | None = None) -> None:
        self._docs = docs
        self._filter = field_filter
        self._limit = limit

    def where(self, filter) -> "FakeQuery":
        assert filter.op_string == "=="
        return FakeQuery(self._docs, filter, self._limit)

    def limit(self, count: int) -> "FakeQuery":
        return FakeQuery(self._docs, self._filter, count)

    def stream(self):
        found = [
            FakeSnapshot(data, FakeDocument(self._docs, doc_id))
            for doc_id, data in self._docs.items()
            if data.get(self._filter.field_path) == self._filter.value
        ]
        return iter(found[: self._limit])


class FakeCollection(FakeQuery):
    def document(self, doc_id: str) -> FakeDocument:
        return FakeDocument(self._docs, doc_id)


class FakeFirestoreClient:
    def __init__(self, **kwargs) -> None:
        self.collections: dict[str, dict] = {}

    def collection(self, name: str) -> FakeCollection:
        return FakeCollection(self.collections.setdefault(name, {}))


class FakeBlob:
    def __init__(self, objects: dict, name: str) -> None:
        self._objects = objects
        self._name = name

    def upload_from_string(self, data: bytes) -> None:
        self._objects[self._name] = data

    def download_as_bytes(self) -> bytes:
        if self._name not in self._objects:
            raise NotFound("ありません")
        return self._objects[self._name]

    def delete(self) -> None:
        if self._name not in self._objects:
            raise NotFound("ありません")
        del self._objects[self._name]


class FakeBucket:
    def __init__(self, objects: dict) -> None:
        self._objects = objects

    def blob(self, name: str) -> FakeBlob:
        return FakeBlob(self._objects, name)


class FakeStorageClient:
    def __init__(self, **kwargs) -> None:
        # バケット名 -> {オブジェクト名: バイト列}
        self.buckets: dict[str, dict[str, bytes]] = {}

    def bucket(self, name: str) -> FakeBucket:
        return FakeBucket(self.buckets.setdefault(name, {}))


class FakeTasksClient:
    def __init__(self, **kwargs) -> None:
        self.tasks: dict[str, dict] = {}
        self.create_calls = 0

    def queue_path(self, project: str, location: str, queue: str) -> str:
        return f"projects/{project}/locations/{location}/queues/{queue}"

    def create_task(self, request: dict) -> dict:
        self.create_calls += 1
        task = request["task"]
        if task["name"] in self.tasks:
            raise AlreadyExists("予約済みです")
        self.tasks[task["name"]] = {"parent": request["parent"], **task}
        return task
