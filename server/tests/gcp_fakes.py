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

    def update(self, fields: dict) -> None:
        # 本物の Firestore と同じく、ないドキュメントは更新できない
        if self._id not in self._docs:
            raise NotFound("ありません")
        self._docs[self._id].update(fields)

    def get(self) -> FakeSnapshot:
        return FakeSnapshot(self._docs.get(self._id))

    def delete(self) -> None:
        # 本物の Firestore と同じく、ないドキュメントを消してもエラーにならない
        self._docs.pop(self._id, None)


class FakeQuery:
    def __init__(
        self,
        docs: dict,
        field_filter=None,
        limit: int | None = None,
        orders: tuple = (),
        start_after: dict | None = None,
    ) -> None:
        self._docs = docs
        self._filter = field_filter
        self._limit = limit
        self._orders = orders
        self._start_after = start_after

    def _copy(self, **changes) -> "FakeQuery":
        args = {
            "field_filter": self._filter,
            "limit": self._limit,
            "orders": self._orders,
            "start_after": self._start_after,
            **changes,
        }
        return FakeQuery(self._docs, **args)

    def where(self, filter) -> "FakeQuery":
        assert filter.op_string == "=="
        return self._copy(field_filter=filter)

    def order_by(self, field_path: str, direction: str = firestore.Query.ASCENDING) -> "FakeQuery":
        return self._copy(orders=(*self._orders, (field_path, direction)))

    def start_after(self, values: dict) -> "FakeQuery":
        # order_by に並べた項目すべての値を、辞書で渡す形だけを真似る
        assert set(values) == {path for path, _ in self._orders}
        return self._copy(start_after=values)

    def limit(self, count: int) -> "FakeQuery":
        return self._copy(limit=count)

    def stream(self):
        found = [
            (doc_id, data)
            for doc_id, data in self._docs.items()
            if self._filter is None or data.get(self._filter.field_path) == self._filter.value
        ]
        # order_by に指定した項目を持たないドキュメントは、本物と同じく結果に出ない
        for path, _ in self._orders:
            found = [(doc_id, data) for doc_id, data in found if path in data]

        # 後ろの項目から順に安定ソートして、複数の項目の並び順を作る
        for path, direction in reversed(self._orders):
            found.sort(
                key=lambda item, path=path: item[1][path],
                reverse=direction == firestore.Query.DESCENDING,
            )
        if self._start_after is not None:
            # 項目ごとの昇順・降順が混ざっても使えるよう、1項目ずつ「後ろか」を比べる
            def is_after(data: dict) -> bool:
                for path, direction in self._orders:
                    value, boundary = data[path], self._start_after[path]
                    if value == boundary:
                        continue
                    return (value < boundary) == (direction == firestore.Query.DESCENDING)
                return False

            found = [item for item in found if is_after(item[1])]
        snapshots = [FakeSnapshot(data, FakeDocument(self._docs, doc_id)) for doc_id, data in found]
        return iter(snapshots[: self._limit])


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
