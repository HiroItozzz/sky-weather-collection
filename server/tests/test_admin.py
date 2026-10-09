import hashlib
from datetime import UTC, datetime, timedelta

from sky_server.admin import create_user, main, revoke_user, run_due_jobs
from sky_server.jobs import LocalTaskScheduler
from sky_server.storage import LocalObservationRepository


def test_撮影者を作るとハッシュだけが保存される(tmp_path):
    repo = LocalObservationRepository(tmp_path)
    user, token = create_user(repo, "alice")
    assert user.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert user.revoked_at is None
    assert user.consent_public is False
    saved = (tmp_path / "users" / f"{user.user_id}.json").read_text()
    assert token not in saved


def test_無効にするとrevoked_atが入る(tmp_path):
    repo = LocalObservationRepository(tmp_path)
    user, _ = create_user(repo, "alice")
    revoked = revoke_user(repo, user.user_id)
    assert revoked.revoked_at is not None
    assert repo.get_user(user.user_id).revoked_at is not None


def test_存在しない撮影者の無効化は失敗する(tmp_path):
    assert revoke_user(LocalObservationRepository(tmp_path), "nope") is None


def test_コマンドで作成と無効化ができる(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SKY_DATA_DIR", str(tmp_path))
    assert main(["create-user", "--name", "bob"]) == 0
    out = capsys.readouterr().out
    user_id = out.splitlines()[0].split(": ")[1]
    assert main(["revoke-user", "--user-id", user_id]) == 0
    assert main(["revoke-user", "--user-id", "nope"]) == 1


class FakeRunner:
    """`run` が呼ばれた job_id を記録する偽物。`errors` の job_id では例外を投げる。"""

    def __init__(self, scheduler, errors=(), reschedule=None):
        self.scheduler = scheduler
        self.errors = errors
        self.reschedule = reschedule or {}
        self.called = []

    def run(self, job_id, now):
        self.called.append(job_id)
        if job_id in self.errors:
            raise RuntimeError("失敗")
        if job_id in self.reschedule:
            self.scheduler.schedule(job_id, self.reschedule[job_id])
        return "ran", None


T0 = datetime(2026, 10, 9, 3, 0, tzinfo=UTC)


def test_期限の来たものだけ古い順に実行して予約を消す(tmp_path, capsys):
    scheduler = LocalTaskScheduler(tmp_path)
    scheduler.schedule("late", T0 + timedelta(minutes=2))
    scheduler.schedule("early", T0 + timedelta(minutes=1))
    scheduler.schedule("future", T0 + timedelta(minutes=3))
    runner = FakeRunner(scheduler)
    assert run_due_jobs(scheduler, runner, T0 + timedelta(minutes=2)) == 0
    assert runner.called == ["early", "late"]
    assert [j for j, _ in scheduler.due(T0 + timedelta(days=1))] == ["future"]
    assert capsys.readouterr().out.splitlines() == ["early ran -", "late ran -"]


def test_実行中に予約し直されたものは消さない(tmp_path):
    scheduler = LocalTaskScheduler(tmp_path)
    scheduler.schedule("job", T0)
    later = T0 + timedelta(minutes=5)
    runner = FakeRunner(scheduler, reschedule={"job": later})
    assert run_due_jobs(scheduler, runner, T0) == 0
    assert scheduler.due(later) == [("job", later)]
    # 同じ起動では再試行を実行しない
    assert runner.called == ["job"]


def test_例外が出ても続けて予約を残し終了コードは1(tmp_path, capsys):
    scheduler = LocalTaskScheduler(tmp_path)
    scheduler.schedule("bad", T0)
    scheduler.schedule("good", T0 + timedelta(minutes=1))
    runner = FakeRunner(scheduler, errors={"bad"})
    assert run_due_jobs(scheduler, runner, T0 + timedelta(minutes=1)) == 1
    assert runner.called == ["bad", "good"]
    assert [j for j, _ in scheduler.due(T0 + timedelta(days=1))] == ["bad"]
    assert "bad error RuntimeError: 失敗" in capsys.readouterr().out


def test_コマンドは予約がなければ何もせず0で終わる(tmp_path, monkeypatch):
    monkeypatch.setenv("SKY_DATA_DIR", str(tmp_path))
    assert main(["run-due-jobs"]) == 0
