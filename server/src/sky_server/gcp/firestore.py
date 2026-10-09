"""Firestore を使う保存先（M4 の GCP 版）。"""

from google.api_core.exceptions import AlreadyExists
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from sky_server.jobs import JobRepository, WeatherJob
from sky_server.models import User
from sky_server.storage import ObservationRepository

USERS = "users"
OBSERVATIONS = "observations"
WEATHER_JOBS = "weather_jobs"
USAGE = "usage"


def _create(client, collection: str, doc_id: str, data: dict) -> bool:
    """ドキュメントを作る。同じ ID がすでにあれば作らず False を返す。"""
    try:
        client.collection(collection).document(doc_id).create(data)
    except AlreadyExists:
        return False
    return True


def _get(client, collection: str, doc_id: str) -> dict | None:
    snapshot = client.collection(collection).document(doc_id).get()
    return snapshot.to_dict() if snapshot.exists else None


class FirestoreObservationRepository(ObservationRepository):
    """撮影者は `users`、観測は `observations` に保存する。"""

    def __init__(self, client=None, project: str | None = None) -> None:
        self._client = client or firestore.Client(project=project)

    def add_user(self, user: User) -> None:
        self.update_user(user)

    def get_user(self, user_id: str) -> User | None:
        data = _get(self._client, USERS, user_id)
        return None if data is None else User.model_validate(data)

    def get_user_by_token_hash(self, token_hash: str) -> User | None:
        query = (
            self._client.collection(USERS)
            .where(filter=FieldFilter("token_hash", "==", token_hash))
            .limit(1)
        )
        for snapshot in query.stream():
            return User.model_validate(snapshot.to_dict())
        return None

    def update_user(self, user: User) -> None:
        self._client.collection(USERS).document(user.user_id).set(user.model_dump(mode="json"))

    def get_observation(self, observation_id: str) -> dict | None:
        return _get(self._client, OBSERVATIONS, observation_id)

    def add_observation(self, record: dict) -> bool:
        return _create(self._client, OBSERVATIONS, record["observation_id"], record)

    def get_daily_count(self, user_id: str, day: str) -> int:
        data = _get(self._client, USAGE, f"{user_id}_{day}")
        return 0 if data is None else data["count"]

    def increment_daily_count(self, user_id: str, day: str) -> None:
        self._client.collection(USAGE).document(f"{user_id}_{day}").set(
            {"user_id": user_id, "day": day, "count": firestore.Increment(1)}, merge=True
        )


class FirestoreJobRepository(JobRepository):
    """ジョブは `weather_jobs` に保存する。実行の重複はローカル版と同じく許容する。"""

    def __init__(self, client=None, project: str | None = None) -> None:
        self._client = client or firestore.Client(project=project)

    def add_job(self, job: WeatherJob) -> bool:
        return _create(self._client, WEATHER_JOBS, job.job_id, job.model_dump(mode="json"))

    def get_job(self, job_id: str) -> WeatherJob | None:
        data = _get(self._client, WEATHER_JOBS, job_id)
        return None if data is None else WeatherJob.model_validate(data)

    def update_job(self, job: WeatherJob) -> None:
        self._client.collection(WEATHER_JOBS).document(job.job_id).set(job.model_dump(mode="json"))
