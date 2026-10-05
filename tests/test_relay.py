import json
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.providers import ProviderError, Providers, Publisher, checked, tiktok_chunks
from app.security import media_signature
from app.store import Store
from app.video import VideoError, prepare, probe

PASSWORD = "a-private-relay-password"
META = {"width": 720, "height": 1280, "duration": 12, "fps": 30, "codec": "h264", "pixel_format": "yuv420p", "audio_codec": "aac"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    def fake_prepare(source, destination, platforms):
        shutil.copyfile(source, destination)
        return META
    monkeypatch.setattr("app.main.prepare", fake_prepare)
    with TestClient(create_app(tmp_path / "data", start_worker=False), base_url="http://localhost", headers={"X-Relay": "1"}) as c:
        yield c


def setup(client):
    assert client.post("/api/setup", json={"password": PASSWORD}).status_code == 200


def upload(client, platforms=None, options=None, request_id=None, caption="A little moment #original"):
    return client.post("/api/jobs", files={"video": ("my-video.mp4", b"valid-test-video", "video/mp4")}, data={
        "caption": caption, "platforms": json.dumps(platforms or ["tiktok", "instagram", "youtube"]),
        "options": json.dumps(options or {"manual": True}), "request_id": request_id or str(uuid.uuid4())})


def connected_store(client):
    store = client.app.state.store
    c = {"base_url": "https://relay.example.com", "tiktok_mode": "draft", "instagram_version": "v24.0"}
    for p in ("tiktok", "instagram", "youtube"):
        c[p + "_client_id"] = p + "-client"
        c[p + "_client_secret"] = p + "-secret"
        store.set("token_" + p, {"access_token": "private-" + p, "refresh_token": "refresh-" + p,
                  "expires_at": time.time() + 86400 * 60, "account_id": "account-" + p, "name": "My " + p, "mode": "draft"})
    store.set("config", c)
    return store


def test_policy_pages_are_public_without_exposing_workspace(client):
    connected_store(client)
    for path, heading in [("/terms", "Terms of Service"), ("/privacy", "Privacy Policy")]:
        response = client.get(path, headers={"Host": "relay.example.com"})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert response.headers["cache-control"] == "no-cache"
        assert "default-src 'self'" in response.headers["content-security-policy"]
        assert heading in response.text
        assert "Spiley" in response.text and "spileyapps@gmail.com" in response.text
        assert not any(secret in response.text for secret in ["private-youtube", "refresh-instagram", "tiktok-secret"])
    assert client.get("/static/legal.css").status_code == 200
    assert client.get("/api/settings").status_code == 401
    assert client.get("/api/jobs").status_code == 401
    assert client.get("/oauth/tiktok/connect").status_code == 401


def test_authentication_and_private_uploads(client):
    assert client.get("/").status_code == 200
    assert client.get("/api/session").json()["setup_required"]
    assert client.get("/api/settings").status_code == 401
    assert client.get("/api/jobs").status_code == 401
    assert client.post("/api/setup", json={"password": "short"}).status_code == 400
    setup(client)
    assert client.post("/api/setup", json={"password": PASSWORD}).status_code == 409
    assert client.get("/api/session").json()["authenticated"]
    assert client.post("/api/logout").status_code == 200
    assert client.get("/api/jobs").status_code == 401
    assert client.post("/api/login", json={"password": "wrong"}).status_code == 401
    assert client.post("/api/login", json={"password": PASSWORD}).status_code == 200


def test_csrf_and_rebinding_rejected(client):
    assert client.post("/api/setup", json={"password": PASSWORD}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 400
    setup(client)
    assert client.post("/api/logout", headers={"Origin": "https://evil.example"}).status_code == 403


def test_lan_workspace_login_and_upload(client):
    client.base_url = "http://192.168.1.10:8088"
    client.headers["Origin"] = "http://192.168.1.10:8088"
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    setup(client)
    assert client.get("/api/session").json()["authenticated"]
    assert upload(client).status_code == 200
    assert client.post("/api/logout", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/settings", json={"base_url": "http://192.168.1.10:8088", "youtube_client_id": "id", "youtube_client_secret": "secret"}).status_code == 200
    callback = client.get("/oauth/youtube/connect", follow_redirects=False)
    assert "connection_error" in callback.headers["location"]
    assert "public+HTTPS" in callback.headers["location"]


@pytest.mark.parametrize("host", ["10.0.0.2", "172.16.0.2", "172.31.255.254", "192.168.1.10", "[fd00::2]"])
def test_private_network_hosts_allowed(client, host):
    assert client.get("/", headers={"Host": host + ":8088"}).status_code == 200


@pytest.mark.parametrize("host", ["evil.example", "8.8.8.8", "172.32.0.2", "192.0.2.1"])
def test_unconfigured_public_hosts_rejected(client, host):
    assert client.get("/", headers={"Host": host + ":8088"}).status_code == 400


def test_manual_upload_no_platform_credentials_and_idempotency(client):
    setup(client)
    request_id = str(uuid.uuid4())
    response = upload(client, request_id=request_id)
    assert response.status_code == 200, response.text
    job = response.json()
    assert job["targets"] == []
    assert job["options"]["manual"]
    assert job["options"]["manual_platforms"] == ["tiktok", "instagram", "youtube"]
    assert "path" not in job and "request_id" not in job
    assert upload(client, request_id=request_id).json()["id"] == job["id"]
    assert len(client.get("/api/jobs").json()["jobs"]) == 1
    assert client.get(f"/api/jobs/{job['id']}/video").content == b"valid-test-video"
    assert client.delete(f"/api/jobs/{job['id']}").status_code == 200
    assert client.get("/api/jobs").json()["jobs"] == []


def test_unconnected_auto_upload_does_not_create_job(client):
    setup(client)
    assert upload(client, options={"manual": False}).status_code == 400
    assert not client.get("/api/jobs").json()["jobs"]


def test_invalid_video_preparation_leaves_no_files(client, monkeypatch):
    setup(client)
    def invalid(*args):
        raise VideoError("Not a valid video.")
    monkeypatch.setattr("app.main.prepare", invalid)
    assert upload(client).status_code == 400
    assert not list((client.app.state.store.root / "videos").iterdir())
    assert not client.get("/api/jobs").json()["jobs"]


def test_caption_utf16_limit(client):
    setup(client)
    assert upload(client, caption="😀" * 1101).status_code == 400
    assert upload(client, caption="😀" * 1100).status_code == 200


def test_credentials_encrypted_and_secrets_not_returned(client):
    setup(client)
    c = {"base_url": "http://localhost:8000", "youtube_client_id": "id", "youtube_client_secret": "very-secret-value"}
    assert client.post("/api/settings", json=c).status_code == 200
    result = client.get("/api/settings").json()
    assert "youtube_client_secret" not in result["config"]
    assert result["accounts"]["youtube"]["configured"]
    assert b"very-secret-value" not in (client.app.state.store.root / "relay.sqlite3-wal").read_bytes()
    assert client.post("/api/settings", json=c | {"youtube_client_secret": ""}).status_code == 200
    assert client.app.state.store.get("config")["youtube_client_secret"] == "very-secret-value"


def test_public_url_validation_and_signed_media(client):
    setup(client)
    assert client.post("/api/settings", json={"base_url": "http://server.example"}).status_code == 400
    assert client.post("/api/settings", json={"base_url": "https://server.example/path"}).status_code == 400
    job = upload(client).json()
    store = client.app.state.store
    expires = int(time.time()) + 60
    signature = media_signature(store, job["id"], expires)
    client.cookies.clear()
    assert client.get(f"/api/jobs/{job['id']}/video").status_code == 401
    assert client.get(f"/media/{job['id']}").status_code == 403
    assert client.get(f"/media/{job['id']}?expires={expires}&signature={signature}").status_code == 200
    assert client.get(f"/media/{job['id']}?expires={expires-100}&signature={signature}").status_code == 403


def test_oauth_state_bound_to_session_and_single_use(client, monkeypatch):
    setup(client)
    client.post("/api/settings", json={"base_url": "http://localhost:8000", "youtube_client_id": "id", "youtube_client_secret": "secret"})
    response = client.get("/oauth/youtube/connect", follow_redirects=False)
    assert response.status_code == 303
    row = client.app.state.store.rows("SELECT * FROM oauth")[0]
    assert "secret" not in row["config"]
    monkeypatch.setattr(client.app.state.providers, "exchange", lambda p, code: None)
    invalid = client.get("/oauth/youtube/callback?state=wrong&code=test", follow_redirects=False)
    assert "connection_error" in invalid.headers["location"]
    callback = f"/oauth/youtube/callback?state={row['state']}&code=test"
    assert "connected=youtube" in client.get(callback, follow_redirects=False).headers["location"]
    assert "connection_error" in client.get(callback, follow_redirects=False).headers["location"]


def test_tiktok_direct_consent_required(client):
    setup(client)
    store = connected_store(client)
    token = store.get("token_tiktok") | {"mode": "direct"}
    store.set("token_tiktok", token)
    assert upload(client, ["tiktok"], {"manual": False}).status_code == 400
    assert upload(client, ["tiktok"], {"manual": False, "tiktok_consent": True, "tiktok_privacy": "SELF_ONLY", "commercial": True, "branded_content": True}).status_code == 400


@pytest.mark.parametrize("size", [1, 4_194_304, 4_999_999, 5_000_000, 5_000_001,
                                 9_999_999, 10_000_000, 10_000_001, 12_500_000, 19_999_999,
                                 20_000_000, 50_000_123, 63_999_999, 64_000_000, 64_000_001, 512_000_000])
def test_tiktok_chunks_match_transfer_protocol(size):
    chunk, count = tiktok_chunks(size)
    sizes = [chunk] * (count - 1) + [size - chunk * (count - 1)]
    assert sum(sizes) == size
    assert count == max(1, size // chunk)
    assert count <= 1000
    if count == 1:
        assert chunk == size
    if size > 64_000_000:
        assert count > 1
    assert all(5_000_000 <= s <= 64_000_000 for s in sizes[:-1])
    assert sizes[-1] <= 128_000_000


@pytest.mark.parametrize("size", [0, -1])
def test_tiktok_rejects_empty_upload(size):
    with pytest.raises(ProviderError, match="empty"):
        tiktok_chunks(size)


@pytest.mark.parametrize("size", [12_500_000, 70_000_123])
def test_tiktok_upload_declaration_matches_transferred_bytes(client, size):
    setup(client)
    store = connected_store(client)
    job = store.job(upload(client, ["tiktok"], {"manual": False}).json()["id"])
    # A prepared video's byte size is what the API needs, irrespective of codec.
    with Path(job["path"]).open("wb") as video:
        video.truncate(size)
    store.execute("UPDATE jobs SET size=? WHERE id=?", (size, job["id"]))
    job = store.job(job["id"])
    source = None
    transferred = 0
    parts = 0

    def handler(request):
        nonlocal source, transferred, parts
        if request.url.path.endswith("/inbox/video/init/"):
            source = json.loads(request.content)["source_info"]
            assert source["video_size"] == size
            assert source["total_chunk_count"] == size // source["chunk_size"]
            if source["total_chunk_count"] == 1:
                assert source["chunk_size"] == size
            return httpx.Response(200, json={"data": {"publish_id": "tik-job", "upload_url": "https://open-upload.tiktokapis.com/video/"}, "error": {"code": "ok"}})
        assert request.method == "PUT" and request.url.host == "open-upload.tiktokapis.com"
        assert source is not None
        length = len(request.content)
        assert int(request.headers["Content-Length"]) == length
        assert request.headers["Content-Range"] == f"bytes {transferred}-{transferred + length - 1}/{size}"
        assert request.headers["Content-Type"] == "video/mp4"
        assert "Transfer-Encoding" not in request.headers
        if parts < source["total_chunk_count"] - 1:
            assert length == source["chunk_size"]
        else:
            assert length == size - transferred
        transferred += length
        parts += 1
        return httpx.Response(201 if transferred == size else 206)

    providers = Providers(store, httpx.Client(transport=httpx.MockTransport(handler)))
    providers.start(job, job["targets"][0])
    assert transferred == size and parts == source["total_chunk_count"]
    assert store.job(job["id"])["targets"][0]["status"] == "processing"


def test_tiktok_http_200_error_is_not_success():
    response = httpx.Response(200, json={"data": {}, "error": {"code": "spam_risk_too_many_posts", "message": "Posting limit reached"}})
    with pytest.raises(ProviderError, match="Posting limit"):
        checked(response, tiktok=True)


def test_all_three_adapters_upload_and_poll_realistic_responses(client):
    setup(client)
    store = connected_store(client)
    result = upload(client, options={"manual": False, "youtube_title": "A short story"})
    assert result.status_code == 200, result.text
    job = store.job(result.json()["id"])
    requests = []
    ig_published = False
    def handler(request):
        nonlocal ig_published
        requests.append(request)
        path = request.url.path
        if path.endswith("/inbox/video/init/"):
            payload = json.loads(request.content)
            assert "post_info" not in payload  # Draft API cannot attach a caption.
            return httpx.Response(200, json={"data": {"publish_id": "tik-job", "upload_url": "https://open-upload.tiktokapis.com/video/"}, "error": {"code": "ok"}})
        if request.url.host == "open-upload.tiktokapis.com":
            assert request.content == b"valid-test-video"
            assert request.headers["Content-Range"] == "bytes 0-15/16"
            return httpx.Response(201)
        if path.endswith("/status/fetch/"):
            return httpx.Response(200, json={"data": {"status": "SEND_TO_USER_INBOX"}, "error": {"code": "ok"}})
        if path == "/upload/youtube/v3/videos" and request.method == "POST":
            payload = json.loads(request.content)
            assert payload["snippet"]["title"] == "A short story"
            assert payload["snippet"]["description"] == job["caption"]
            return httpx.Response(200, headers={"Location": "https://www.googleapis.com/youtube-upload/session"})
        if path == "/youtube-upload/session":
            assert request.content == b"valid-test-video"
            return httpx.Response(200, json={"id": "yt-video", "status": {"privacyStatus": "private"}})
        if path == "/youtube/v3/videos":
            return httpx.Response(200, json={"items": [{"id": "yt-video", "status": {"privacyStatus": "private", "uploadStatus": "processed"}, "processingDetails": {"processingStatus": "succeeded"}}]})
        if path.endswith("/account-instagram/media"):
            assert b"media_type=REELS" in request.content
            assert b"video_url=https%3A%2F%2Frelay.example.com%2Fmedia%2F" in request.content
            return httpx.Response(200, json={"id": "ig-container"})
        if path.endswith("/ig-container"):
            return httpx.Response(200, json={"status_code": "PUBLISHED" if ig_published else "FINISHED"})
        if path.endswith("/media_publish"):
            ig_published = True
            return httpx.Response(200, json={"id": "ig-media"})
        if path.endswith("/ig-media"):
            return httpx.Response(200, json={"permalink": "https://www.instagram.com/reel/example/"})
        raise AssertionError(str(request.url))
    providers = Providers(store, httpx.Client(transport=httpx.MockTransport(handler)))
    for target in job["targets"]:
        providers.start(job, target)
    for target in store.job(job["id"])["targets"]:
        providers.poll(job, target)
    targets = {t["platform"]: t for t in store.job(job["id"])["targets"]}
    assert targets["tiktok"]["status"] == "draft"
    assert targets["youtube"]["status"] == "restricted"
    assert targets["youtube"]["result"]["privacy"] == "private"
    assert targets["instagram"]["status"] == "published"
    assert sum(r.url.path.endswith("/media_publish") for r in requests) == 1


def test_ambiguous_instagram_publish_is_not_replayed(client):
    setup(client)
    store = connected_store(client)
    job = store.job(upload(client, ["instagram"], {"manual": False}).json()["id"])
    store.update_target(job["id"], "instagram", status="attention", remote_id="ig-container")
    calls = []
    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json={"status_code": "FINISHED"})
    providers = Providers(store, httpx.Client(transport=httpx.MockTransport(handler)))
    providers.check_only(job, store.job(job["id"])["targets"][0])
    assert calls == ["GET"]
    assert store.job(job["id"])["targets"][0]["status"] == "attention"


def test_worker_restart_does_not_duplicate_uploads(client):
    setup(client)
    store = connected_store(client)
    job = store.job(upload(client, ["youtube"], {"manual": False}).json()["id"])
    store.update_target(job["id"], "youtube", status="uploading")
    store.recover()
    assert store.job(job["id"])["targets"][0]["status"] == "attention"
    assert not store.rows("SELECT * FROM targets WHERE status='queued'")
    assert client.delete("/api/jobs/" + job["id"]).status_code == 409
    assert client.post(f"/api/jobs/{job['id']}/youtube/reviewed").status_code == 200
    assert store.job(job["id"])["targets"][0]["status"] == "reviewed"
    assert client.delete("/api/jobs/" + job["id"]).status_code == 200


@pytest.mark.parametrize("wrapped", [True, False])
def test_instagram_oauth_exchange_accepts_response_shapes(client, wrapped):
    setup(client)
    store = connected_store(client)
    short = {"access_token": "short", "user_id": "ig-id", "permissions": ["instagram_business_basic", "instagram_business_content_publish"]}
    def handler(request):
        if request.url.host == "api.instagram.com":
            assert request.method == "POST" and b"grant_type=authorization_code" in request.content
            return httpx.Response(200, json={"data": [short]} if wrapped else short)
        if request.url.path == "/access_token":
            assert request.url.params["grant_type"] == "ig_exchange_token"
            return httpx.Response(200, json={"access_token": "long-lived", "expires_in": 5184000})
        if request.url.path == "/me":
            assert request.headers["Authorization"] == "Bearer long-lived"
            return httpx.Response(200, json={"user_id": "ig-id", "username": "mycreator"})
        raise AssertionError(str(request.url))
    providers = Providers(store, httpx.Client(transport=httpx.MockTransport(handler)))
    providers.exchange("instagram", "code")
    token = store.get("token_instagram")
    assert token["account_id"] == "ig-id" and token["name"] == "mycreator"
    assert token["expires_at"] > time.time() + 86400 * 59


def test_token_refresh_keeps_account_and_rotates_refresh_token(client):
    setup(client)
    store = connected_store(client)
    store.set("token_youtube", store.get("token_youtube") | {"expires_at": time.time() - 1})
    def handler(request):
        assert b"grant_type=refresh_token" in request.content
        assert b"refresh_token=refresh-youtube" in request.content
        return httpx.Response(200, json={"access_token": "new-token", "expires_in": 3600, "refresh_token": "new-refresh"})
    providers = Providers(store, httpx.Client(transport=httpx.MockTransport(handler)))
    token = providers.token("youtube", "account-youtube")
    assert token["access_token"] == "new-token"
    assert token["refresh_token"] == "new-refresh"
    assert token["account_id"] == "account-youtube"
    with pytest.raises(ProviderError, match="account changed"):
        providers.token("youtube", "different-account")


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg is installed in the Docker image")
def test_real_video_preparation_and_shorts_validation(tmp_path):
    source, prepared = tmp_path / "source.webm", tmp_path / "prepared.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "color=c=orange:s=360x640:r=30:d=3.2",
                    "-c:v", "libvpx-vp9", "-y", str(source)], check=True, capture_output=True)
    meta = prepare(source, prepared, ["youtube", "instagram", "tiktok"])
    assert meta["codec"] == "h264" and meta["pixel_format"] == "yuv420p"
    assert meta["height"] == 640 and meta["duration"] >= 3
    assert prepared.stat().st_size > 0
    landscape = tmp_path / "landscape.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "color=s=640x360:d=3.2", "-c:v", "libx264", "-y", str(landscape)], check=True, capture_output=True)
    with pytest.raises(VideoError, match="Shorts needs"):
        prepare(landscape, tmp_path / "rejected.mp4", ["youtube"])
