"""Cloud Storage を使う画像・天気データの保存先（M4 の GCP 版）。"""

from google.api_core.exceptions import NotFound
from google.cloud import storage

from sky_server.storage import BlobStore


class GcsBlobStore(BlobStore):
    """バケットの中の `prefix` の下に保存する。画像は `images/`、天気データは `weather/`。"""

    def __init__(self, bucket: str, prefix: str, client=None, project: str | None = None) -> None:
        self._bucket = (client or storage.Client(project=project)).bucket(bucket)
        self._prefix = prefix

    def put(self, key: str, data: bytes) -> None:
        self._bucket.blob(self._prefix + key).upload_from_string(data)

    def get(self, key: str) -> bytes | None:
        try:
            return self._bucket.blob(self._prefix + key).download_as_bytes()
        except NotFound:
            return None
