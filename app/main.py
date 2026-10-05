import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode, urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .providers import PLATFORMS, ProviderError, Providers, Publisher
from .security import local_host, media_signature, password_hash, password_matches
from .store import Store
from .video import VideoError, prepare

STATIC = Path(__file__).parent / "static"


class PasswordInput(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class SettingsInput(BaseModel):
    base_url: str = Field(max_length=300)
    tiktok_mode: str = "draft"
    instagram_version: str = "v24.0"
    youtube_client_id: str = Field(default="", max_length=300)
    youtube_client_secret: str = Field(default="", max_length=500)
    instagram_client_id: str = Field(default="", max_length=300)
    instagram_client_secret: str = Field(default="", max_length=500)
    tiktok_client_id: str = Field(default="", max_length=300)
    tiktok_client_secret: str = Field(default="", max_length=500)


class PostOptions(BaseModel):
    manual: bool = False
    manual_platforms: list[str] = Field(default_factory=list)
    youtube_title: str = Field(default="", max_length=100)
    youtube_privacy: str = "public"
    made_for_kids: bool = False
    ai_generated: bool = False
    tiktok_privacy: str = ""
    tiktok_mode: str = "draft"
    allow_comment: bool = False
    allow_duet: bool = False
    allow_stitch: bool = False
    commercial: bool = False
    own_brand: bool = False
    branded_content: bool = False
    tiktok_consent: bool = False


def public_job(job):
    return {k: v for k, v in job.items() if k not in ("path", "request_id")} | {
        "targets": [{k: v for k, v in t.items() if k not in ("account", "next_check")} for t in job["targets"]]
    }


def create_app(data_dir=None, start_worker=True):
    @asynccontextmanager
    async def lifespan(app):
        store = Store(data_dir or os.getenv("DATA_DIR", "data"))
        providers = Providers(store)
        publisher = Publisher(store, providers)
        app.state.store, app.state.providers, app.state.publisher = store, providers, publisher
        app.state.login_attempts = {}
        app.state.upload_lock = asyncio.Lock()
        store.recover()
        if start_worker:
            publisher.thread.start()
        try:
            yield
        finally:
            if start_worker:
                publisher.close()
            else:
                providers.client.close()
                store.close()

    app = FastAPI(title="Relay", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def boundary(request, call_next):
        if request.url.path == "/health":
            return await call_next(request)
        store = request.app.state.store
        config = store.get("config", {"base_url": "http://localhost:8088"})
        allowed_hosts = {"localhost", "127.0.0.1", "::1", urlparse(config["base_url"]).hostname}
        if request.url.hostname not in allowed_hosts and not local_host(request.url.hostname):
            return JSONResponse({"detail": "Host is not configured. Open Relay using localhost or your server's LAN IP and set the public URL in Connections."}, 400)
        path = request.url.path
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            origin = request.headers.get("origin")
            expected = {str(request.base_url).rstrip("/"), config["base_url"].rstrip("/")}
            if (origin and origin.rstrip("/") not in expected) or (not origin and request.headers.get("x-relay") != "1"):
                return JSONResponse({"detail": "Request origin could not be verified. Reload the page."}, 403)
        public = {"/api/session", "/api/setup", "/api/login"}
        if (path.startswith("/api/") and path not in public) or path.startswith("/oauth/"):
            session = store.session(request.cookies.get("relay_session"))
            if not session:
                return JSONResponse({"detail": "Sign in to Relay."}, 401)
            request.state.session = session
        if path == "/api/jobs" and request.method == "POST":
            length = request.headers.get("content-length")
            try:
                size = int(length) if length else 0
            except ValueError:
                size = 0
            if size <= 0:
                return JSONResponse({"detail": "This upload needs a Content-Length header."}, 411)
            if size > (int(os.getenv("MAX_UPLOAD_MB", "512")) + 2) * 1024 * 1024:
                return JSONResponse({"detail": "Video exceeds the configured upload limit."}, 413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if path.startswith(("/api/", "/oauth/", "/media/")):
            response.headers["Cache-Control"] = "private, no-store"
        return response

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/")
    def home():
        return FileResponse(STATIC / "index.html")

    @app.get("/terms")
    def terms():
        return FileResponse(STATIC / "terms.html", headers={"Cache-Control": "no-cache"})

    @app.get("/privacy")
    def privacy():
        return FileResponse(STATIC / "privacy.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    def session_response(request):
        response = JSONResponse({"ok": True})
        response.set_cookie("relay_session", request.app.state.store.new_session(), max_age=86400 * 7,
                            httponly=True, samesite="lax", secure=request.url.scheme == "https")
        return response

    @app.get("/api/session")
    def session(request: Request):
        store = request.app.state.store
        return {"setup_required": not bool(store.get("password")),
                "authenticated": bool(store.session(request.cookies.get("relay_session")))}

    @app.post("/api/setup")
    def setup(body: PasswordInput, request: Request):
        store = request.app.state.store
        with store.lock:
            if store.get("password"):
                raise HTTPException(409, "Relay is already set up. Sign in instead.")
            if len(body.password) < 12:
                raise HTTPException(400, "Choose a password of at least 12 characters.")
            store.set("password", password_hash(body.password))
        return session_response(request)

    @app.post("/api/login")
    def login(body: PasswordInput, request: Request):
        store = request.app.state.store
        ip = request.client.host if request.client else "local"
        with store.lock:
            attempts = [t for t in request.app.state.login_attempts.get(ip, []) if t > time.time() - 600]
            request.app.state.login_attempts[ip] = attempts
            if len(attempts) >= 8:
                raise HTTPException(429, "Too many login attempts. Try again in 10 minutes.")
            stored = store.get("password")
            if not stored or not password_matches(body.password, stored):
                attempts.append(time.time())
                raise HTTPException(401, "Incorrect password.")
            request.app.state.login_attempts.pop(ip, None)
        return session_response(request)

    @app.post("/api/logout")
    def logout(request: Request):
        request.app.state.store.execute("DELETE FROM sessions WHERE hash=?", (request.state.session,))
        response = JSONResponse({"ok": True})
        response.delete_cookie("relay_session")
        return response

    @app.get("/api/settings")
    def settings(request: Request):
        store, providers = request.app.state.store, request.app.state.providers
        c = providers.config()
        result = {k: v for k, v in c.items() if not k.endswith("_secret")}
        accounts = {}
        for p in PLATFORMS:
            token = store.get("token_" + p)
            accounts[p] = {"configured": bool(c.get(p + "_client_id") and c.get(p + "_client_secret")),
                           "connected": bool(token), "name": token.get("name", "") if token else "",
                           "expires_at": token.get("expires_at") if token else None,
                           "mode": token.get("mode") if token else None,
                           "callback_url": providers.callback_url(p)}
        return {"config": result, "accounts": accounts, "max_upload_mb": int(os.getenv("MAX_UPLOAD_MB", "512"))}

    @app.post("/api/settings")
    def save_settings(body: SettingsInput, request: Request):
        c = body.model_dump()
        p = urlparse(c["base_url"])
        if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password or p.query or p.fragment or p.path not in ("", "/"):
            raise HTTPException(400, "Use a complete URL without a path, e.g. https://relay.example.com.")
        if p.scheme == "http" and not local_host(p.hostname):
            raise HTTPException(400, "Use HTTPS for a public URL. Localhost and local-network IPs may use HTTP.")
        if c["tiktok_mode"] not in ("draft", "direct") or not re.fullmatch(r"v\d+\.0", c["instagram_version"]):
            raise HTTPException(400, "Invalid platform settings.")
        c["base_url"] = c["base_url"].rstrip("/")
        store = request.app.state.store
        old = request.app.state.providers.config()
        with request.app.state.providers.token_lock:
            for platform in PLATFORMS:
                key = platform + "_client_secret"
                if not c[key]:
                    c[key] = old.get(key, "")
                changed = any(c.get(k) != old.get(k, "") for k in (platform + "_client_id", key))
                if platform == "tiktok":
                    changed |= c["tiktok_mode"] != old.get("tiktok_mode", "draft")
                if changed:
                    store.set("token_" + platform, None)
            store.set("config", c)
        return {"ok": True}

    @app.post("/api/accounts/{platform}/disconnect")
    def disconnect(platform: str, request: Request):
        if platform not in PLATFORMS:
            raise HTTPException(404)
        with request.app.state.providers.token_lock:
            request.app.state.store.set("token_" + platform, None)
        return {"ok": True, "message": "Disconnected from Relay. You can also revoke this app in the platform's account settings."}

    @app.get("/oauth/{platform}/connect")
    def connect(platform: str, request: Request):
        if platform not in PLATFORMS:
            raise HTTPException(404)
        providers, store = request.app.state.providers, request.app.state.store
        c = providers.config()
        if not c.get(platform + "_client_id") or not c.get(platform + "_client_secret"):
            return RedirectResponse("/?" + urlencode({"connection_error": "Save the app credentials before connecting."}), 303)
        if platform != "youtube" and urlparse(c["base_url"]).scheme != "https":
            return RedirectResponse("/?" + urlencode({"connection_error": "TikTok and Instagram need an HTTPS callback URL. Set your public URL first."}), 303)
        base = urlparse(c["base_url"])
        if platform == "youtube" and base.scheme == "http" and base.hostname not in ("localhost", "127.0.0.1", "::1"):
            return RedirectResponse("/?" + urlencode({"connection_error": "YouTube needs a localhost or public HTTPS callback. For a remote server, set a public HTTPS URL, or access Relay through localhost port forwarding."}), 303)
        state = secrets.token_urlsafe(32)
        fingerprint = hashlib.sha256(json.dumps(c, sort_keys=True).encode()).hexdigest()
        store.execute("DELETE FROM oauth WHERE expires<?", (time.time(),))
        store.execute("INSERT INTO oauth VALUES (?,?,?,?,?)", (state, request.state.session, platform, time.time() + 600, fingerprint))
        return RedirectResponse(providers.authorize_url(platform, state), 303)

    @app.get("/oauth/{platform}/callback")
    def callback(platform: str, request: Request, state: str = "", code: str = "", error: str = ""):
        store, providers = request.app.state.store, request.app.state.providers
        with store.lock:
            rows = store.rows("SELECT * FROM oauth WHERE state=? AND session=? AND platform=? AND expires>?",
                              (state, request.state.session, platform, time.time()))
            if not rows:
                return RedirectResponse("/?" + urlencode({"connection_error": "Connection expired or could not be verified. Try Connect again."}), 303)
            store.execute("DELETE FROM oauth WHERE state=?", (state,))
        try:
            if error or not code:
                raise ProviderError("Account connection was declined. You can try again in Connections.")
            fingerprint = hashlib.sha256(json.dumps(providers.config(), sort_keys=True).encode()).hexdigest()
            if not hmac.compare_digest(fingerprint, rows[0]["config"]):
                raise ProviderError("Settings changed during sign-in. Try connecting again.")
            with providers.token_lock:
                providers.exchange(platform, code)
        except (ProviderError, httpx.HTTPError, KeyError, ValueError):
            return RedirectResponse("/?" + urlencode({"connection_error": "Connection failed. Check app credentials, callback URL, test users, and granted permissions, then reconnect."}), 303)
        return RedirectResponse("/?" + urlencode({"connected": platform}), 303)

    @app.get("/api/tiktok/creator")
    def creator(request: Request):
        try:
            return request.app.state.providers.creator()
        except (ProviderError, httpx.HTTPError) as exc:
            raise HTTPException(400, str(exc) if isinstance(exc, ProviderError) else "Could not load TikTok account settings. Try reconnecting.") from None

    @app.get("/api/jobs")
    def jobs(request: Request):
        store = request.app.state.store
        return {"jobs": [public_job(store.job(r["id"])) for r in store.rows("SELECT id FROM jobs ORDER BY created DESC LIMIT 100")],
                "storage_bytes": sum(r["size"] for r in store.rows("SELECT size FROM jobs"))}

    @app.post("/api/jobs")
    async def post(request: Request):
        # One local preparation at a time keeps ffmpeg and disk use bounded.
        async with request.app.state.upload_lock:
            return await receive_post(request)

    async def receive_post(request):
        store, providers = request.app.state.store, request.app.state.providers
        async with request.form(max_files=1, max_fields=5) as form:
            request_id = str(form.get("request_id", ""))
            try:
                uuid.UUID(request_id)
            except ValueError:
                raise HTTPException(400, "Missing upload request ID. Reload the page.") from None
            duplicate = store.rows("SELECT id FROM jobs WHERE request_id=?", (request_id,))
            if duplicate:
                return public_job(store.job(duplicate[0]["id"]))
            caption = str(form.get("caption", "")).strip()
            if not caption or len(caption.encode("utf-16-le")) // 2 > 2200:
                raise HTTPException(400, "Write a caption of 1–2,200 characters.")
            try:
                platforms = json.loads(str(form.get("platforms", "[]")))
                options = PostOptions.model_validate_json(str(form.get("options", "{}")))
            except (ValueError, TypeError):
                raise HTTPException(400, "Invalid post options.") from None
            if not isinstance(platforms, list) or not platforms or len(platforms) > 3 or any(not isinstance(p, str) or p not in PLATFORMS for p in platforms):
                raise HTTPException(400, "Select at least one valid destination.")
            platforms = list(dict.fromkeys(platforms))
            accounts = {}
            for p in platforms:
                if options.manual:
                    continue
                token = store.get("token_" + p)
                if not token:
                    raise HTTPException(400, "Connect " + p + " first, or select Save for manual upload.")
                accounts[p] = token["account_id"]
            if "youtube" in platforms:
                options.youtube_title = (options.youtube_title.strip() or caption.splitlines()[0][:100]).replace("<", "").replace(">", "")
                if not options.youtube_title or options.youtube_privacy not in ("public", "unlisted", "private"):
                    raise HTTPException(400, "Choose a YouTube title and valid visibility.")
            if "instagram" in platforms and not options.manual:
                try:
                    providers.media_url("check")
                except ProviderError as exc:
                    raise HTTPException(400, str(exc)) from None
            if "tiktok" in platforms and not options.manual:
                mode = store.get("token_tiktok").get("mode", "draft")
                options.tiktok_mode = mode
                if mode == "direct":
                    if not options.tiktok_consent or not options.tiktok_privacy:
                        raise HTTPException(400, "Choose TikTok visibility and agree to TikTok's posting terms.")
                    if options.commercial and not (options.own_brand or options.branded_content):
                        raise HTTPException(400, "Select your brand, branded content, or both.")
                    if options.branded_content and options.tiktok_privacy == "SELF_ONLY":
                        raise HTTPException(400, "TikTok branded content cannot have Only me visibility.")
                    if not options.commercial:
                        options.own_brand = options.branded_content = False
            upload = form.get("video")
            if not upload or not hasattr(upload, "read") or Path(upload.filename or "").suffix.lower() not in (".mp4", ".mov", ".webm"):
                raise HTTPException(400, "Choose an MP4, MOV, or WebM video.")
            job_id = uuid.uuid4().hex
            source = store.root / "videos" / (job_id + ".incoming")
            destination = store.root / "videos" / (job_id + ".mp4")
            try:
                size = 0
                with source.open("xb") as output:
                    while chunk := await upload.read(1024 * 1024):
                        size += len(chunk)
                        if size > int(os.getenv("MAX_UPLOAD_MB", "512")) * 1024 * 1024:
                            raise HTTPException(413, "Video exceeds the upload limit.")
                        output.write(chunk)
                if not size:
                    raise HTTPException(400, "The video file is empty.")
                meta = await asyncio.to_thread(prepare, source, destination, platforms)
                if destination.stat().st_size > int(os.getenv("MAX_UPLOAD_MB", "512")) * 1024 * 1024:
                    raise HTTPException(413, "Prepared video exceeds the upload limit. Export a smaller MP4.")
                options.manual_platforms = platforms if options.manual else []
                job = {"id": job_id, "request_id": request_id, "filename": Path(upload.filename.replace("\\", "/")).name[:200],
                       "caption": caption, "path": str(destination), "size": destination.stat().st_size, "meta": meta, "options": options.model_dump()}
                store.create_job(job, [] if options.manual else platforms, accounts)
                return public_job(store.job(job_id))
            except VideoError as exc:
                destination.unlink(missing_ok=True)
                raise HTTPException(400, str(exc)) from None
            except BaseException:
                destination.unlink(missing_ok=True)
                raise
            finally:
                source.unlink(missing_ok=True)

    @app.post("/api/jobs/{job_id}/{platform}/check")
    def check_job(job_id: str, platform: str, request: Request):
        store = request.app.state.store
        with request.app.state.publisher.operation_lock:
            job = store.job(job_id)
            target = next((t for t in job["targets"] if t["platform"] == platform), None) if job else None
            if not target:
                raise HTTPException(404)
            if target["status"] not in ("attention", "processing") or not target["remote_id"]:
                raise HTTPException(400, "There is no pending remote upload to check.")
            try:
                request.app.state.providers.check_only(job, target)
            except (ProviderError, httpx.HTTPError) as exc:
                raise HTTPException(400, str(exc) if isinstance(exc, ProviderError) else "Could not check the platform. Try later.") from None
            return public_job(store.job(job_id))

    @app.get("/api/jobs/{job_id}/video")
    def saved_video(job_id: str, request: Request):
        job = request.app.state.store.job(job_id)
        if not job:
            raise HTTPException(404)
        return FileResponse(job["path"], media_type="video/mp4", filename=Path(job["filename"]).stem + ".mp4")

    @app.post("/api/jobs/{job_id}/{platform}/reviewed")
    def reviewed(job_id: str, platform: str, request: Request):
        store = request.app.state.store
        with request.app.state.publisher.operation_lock:
            job = store.job(job_id)
            target = next((t for t in job["targets"] if t["platform"] == platform), None) if job else None
            if not target:
                raise HTTPException(404)
            if target["status"] != "attention":
                raise HTTPException(409, "Only uncertain uploads can be marked as manually reviewed.")
            store.update_target(job_id, platform, status="reviewed", error="", result={
                "message": "Checked manually by you. Relay did not verify whether it was posted. You may now remove the local copy."})
            return public_job(store.job(job_id))

    @app.delete("/api/jobs/{job_id}")
    def delete_job(job_id: str, request: Request):
        store = request.app.state.store
        with request.app.state.publisher.operation_lock:
            job = store.job(job_id)
            if not job:
                raise HTTPException(404)
            if any(t["status"] in ("queued", "uploading", "processing", "publishing", "attention") for t in job["targets"]):
                raise HTTPException(409, "Keep this video until pending uploads are resolved. Disconnecting does not cancel a remote post.")
            store.execute("DELETE FROM targets WHERE job_id=?", (job_id,))
            store.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            store.execute("DELETE FROM settings WHERE key=?", ("upload_" + job_id + "_youtube",))
            Path(job["path"]).unlink(missing_ok=True)
            return {"ok": True}

    @app.get("/media/{job_id}")
    def media(job_id: str, request: Request, expires: int = 0, signature: str = ""):
        store = request.app.state.store
        if expires < time.time() or not hmac.compare_digest(signature, media_signature(store, job_id, expires)):
            raise HTTPException(403, "Media link expired or invalid.")
        job = store.job(job_id)
        if not job:
            raise HTTPException(404)
        return FileResponse(job["path"], media_type="video/mp4")

    return app


app = create_app()
