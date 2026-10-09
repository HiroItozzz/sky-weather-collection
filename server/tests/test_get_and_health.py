from conftest import OBSERVATION_ID


def test_healthzは認証なしでokを返す(api):
    res = api.client.get("/healthz")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_自分の観測を取得できる(api, token):
    api.put(token)
    res = api.client.get(f"/v1/observations/{OBSERVATION_ID}", headers=_auth(token))
    assert res.status_code == 200
    body = res.json()
    assert body["observation_id"] == OBSERVATION_ID
    assert body["received_at"] == api.repository.get_observation(OBSERVATION_ID)["received_at"]


def test_存在しない観測は404(api, token):
    res = api.client.get(f"/v1/observations/{OBSERVATION_ID}", headers=_auth(token))
    assert res.status_code == 404


def test_他人の観測は404(api, token):
    api.put(token)
    _, other_token = api.new_user("other")
    res = api.client.get(f"/v1/observations/{OBSERVATION_ID}", headers=_auth(other_token))
    assert res.status_code == 404
    # 存在しない場合と同じ応答になる
    missing = api.client.get(
        "/v1/observations/00000000-0000-4000-8000-000000000000", headers=_auth(other_token)
    )
    assert res.json() == missing.json()


def test_GETも認証が必要(api, token):
    api.put(token)
    assert api.client.get(f"/v1/observations/{OBSERVATION_ID}").status_code == 401


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}
