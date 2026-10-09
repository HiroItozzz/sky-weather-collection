"""設定から、保存先と予約の組を作る。"""

from dataclasses import dataclass
from pathlib import Path

from sky_server.config import get_backend_name, get_data_dir, get_gcp_settings
from sky_server.jobs import (
    JobRepository,
    LocalJobRepository,
    LocalTaskScheduler,
    TaskScheduler,
)
from sky_server.storage import (
    BlobStore,
    LocalBlobStore,
    LocalObservationRepository,
    ObservationRepository,
)


@dataclass
class Backend:
    repository: ObservationRepository
    blob_store: BlobStore
    weather_store: BlobStore
    job_repository: JobRepository
    scheduler: TaskScheduler


def build_backend(data_dir: Path | None = None) -> Backend:
    """`SKY_BACKEND` に合わせて作る。`data_dir` はローカル版だけで使う。"""
    if get_backend_name() == "gcp":
        return _build_gcp()
    data_dir = data_dir or get_data_dir()
    return Backend(
        repository=LocalObservationRepository(data_dir),
        blob_store=LocalBlobStore(data_dir),
        weather_store=LocalBlobStore(data_dir, "weather"),
        job_repository=LocalJobRepository(data_dir),
        scheduler=LocalTaskScheduler(data_dir),
    )


def _build_gcp() -> Backend:
    settings = get_gcp_settings()
    # google.cloud.* は gcp のときだけ読み込む
    from google.cloud import firestore

    from sky_server.gcp.firestore import FirestoreJobRepository, FirestoreObservationRepository
    from sky_server.gcp.gcs import GcsBlobStore
    from sky_server.gcp.tasks import CloudTasksScheduler

    client = firestore.Client(project=settings.project)
    return Backend(
        repository=FirestoreObservationRepository(client),
        blob_store=GcsBlobStore(settings.bucket, "images/", project=settings.project),
        weather_store=GcsBlobStore(settings.bucket, "weather/", project=settings.project),
        job_repository=FirestoreJobRepository(client),
        scheduler=CloudTasksScheduler(
            settings.project,
            settings.tasks_location,
            settings.tasks_queue,
            settings.tasks_target_url,
            settings.tasks_service_account,
        ),
    )
