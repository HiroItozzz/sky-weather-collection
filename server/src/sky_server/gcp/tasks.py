"""Cloud Tasks で天気ジョブの実行を予約する（M4 の GCP 版）。"""

import json
from datetime import datetime

from google.api_core.exceptions import AlreadyExists
from google.cloud import tasks_v2

from sky_server.jobs import TaskScheduler


class CloudTasksScheduler(TaskScheduler):
    def __init__(
        self,
        project: str,
        location: str,
        queue: str,
        target_url: str,
        service_account: str,
        client=None,
    ) -> None:
        self._client = client or tasks_v2.CloudTasksClient()
        self._queue_path = self._client.queue_path(project, location, queue)
        self._target_url = target_url
        self._service_account = service_account

    def schedule(self, job_id: str, run_at: datetime) -> None:
        """キューにタスクを作る。同じ予約がすでにあれば（名前が同じなら）成功とみなす。"""
        task = {
            # 同じ予約を二重に作らないため、名前に job_id と実行時刻を入れる
            "name": f"{self._queue_path}/tasks/{job_id}-{int(run_at.timestamp())}",
            "schedule_time": run_at,
            "http_request": {
                "http_method": tasks_v2.HttpMethod.POST,
                "url": self._target_url,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"job_id": job_id}).encode(),
                "oidc_token": {
                    "service_account_email": self._service_account,
                    "audience": self._target_url,
                },
            },
        }
        try:
            self._client.create_task(request={"parent": self._queue_path, "task": task})
        except AlreadyExists:
            return
