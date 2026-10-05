"""Official platform adapters. Remote writes are never automatically replayed."""
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse

import httpx

PLATFORMS = ("tiktok", "instagram", "youtube")
TIKTOK = "https://open.tiktokapis.com/v2"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"


class ProviderError(Exception):
    pass


def safe_message(data, status):
    error = data.get("error", {}) if isinstance(data, dict) else {}
    if isinstance(error, dict):
        message = error.get("message") or error.get("code")
    else:
        message = data.get("error_description") or error
    text = str(message or f"Platform request failed (HTTP {status}).")[:700]
    text = re.sub(r"https?://\S+", "[platform URL]", text)
    text = re.sub(r"(?i)(access_token|refresh_token|client_secret|upload_token)\s*[=:]\s*\S+", r"\1=[hidden]", text)
    return text


def checked(response, tiktok=False):
    try:
        data = response.json()
    except ValueError:
        raise ProviderError(f"Platform returned an unexpected response (HTTP {response.status_code}).") from None
    if not isinstance(data, dict):
        raise ProviderError("Platform returned an unexpected response.")
    error = data.get("error")
    if response.is_error or (tiktok and (not isinstance(error, dict) or error.get("code") != "ok")) or (not tiktok and error):
        raise ProviderError(safe_message(data, response.status_code))
    return data.get("data", {}) if tiktok else data


def validated_upload_url(url, domains):
    p = urlparse(url)
    if p.scheme != "https" or p.username or p.password or not any(
        p.hostname == d or (p.hostname or "").endswith("." + d) for d in domains
    ):
        raise ProviderError("Platform returned an untrusted upload URL.")
    return url


def tiktok_chunks(size):
    if size <= 0:
        raise ProviderError("The saved video file is empty.")
    # For a single PUT, TikTok requires the declared chunk to be the whole file.
    if size <= 64_000_000:
        return size, 1
    # TikTok uses FLOOR(size/chunk), merging remainder into the last chunk.
    chunk = 10_000_000
    return chunk, size // chunk


def file_chunks(path, offset=0, length=None):
    with Path(path).open("rb") as f:
        f.seek(offset)
        remaining = length if length is not None else Path(path).stat().st_size - offset
        while remaining:
            chunk = f.read(min(1024 * 1024, remaining))
            if not chunk:
                raise ProviderError("The saved video file is incomplete.")
            remaining -= len(chunk)
            yield chunk


class Providers:
    def __init__(self, store, client=None):
        self.store = store
        self.client = client or httpx.Client(timeout=httpx.Timeout(90, connect=20), follow_redirects=False)
        self.token_lock = threading.RLock()

    def config(self):
        return self.store.get("config", {"base_url": "http://localhost:8088", "tiktok_mode": "draft", "instagram_version": "v24.0"})

    def callback_url(self, platform):
        return self.config()["base_url"].rstrip("/") + "/oauth/" + platform + "/callback"

    def authorize_url(self, platform, state):
        c = self.config()
        common = {"client_id": c[platform + "_client_id"], "redirect_uri": self.callback_url(platform),
                  "response_type": "code", "state": state}
        if platform == "youtube":
            common.update(scope="https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly",
                          access_type="offline", prompt="consent", include_granted_scopes="true")
            return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(common)
        if platform == "instagram":
            common.update(scope="instagram_business_basic,instagram_business_content_publish", enable_fb_login="0", force_authentication="1")
            return "https://www.instagram.com/oauth/authorize?" + urlencode(common)
        common["client_key"] = common.pop("client_id")
        common["scope"] = "user.info.basic," + ("video.publish" if c.get("tiktok_mode") == "direct" else "video.upload")
        return "https://www.tiktok.com/v2/auth/authorize/?" + urlencode(common)

    def exchange(self, platform, code):
        c = self.config()
        payload = {"client_id": c[platform + "_client_id"], "client_secret": c[platform + "_client_secret"],
                   "code": code, "grant_type": "authorization_code", "redirect_uri": self.callback_url(platform)}
        if platform == "youtube":
            token = checked(self.client.post(GOOGLE_TOKEN, data=payload))
            identity = checked(self.client.get("https://www.googleapis.com/youtube/v3/channels",
                               params={"part": "snippet", "mine": "true"}, headers={"Authorization": "Bearer " + token["access_token"]}))
            if not identity.get("items"):
                raise ProviderError("This Google account has no YouTube channel. Create a channel, then reconnect.")
            token.update(account_id=identity["items"][0]["id"], name=identity["items"][0]["snippet"]["title"])
            if not token.get("refresh_token"):
                raise ProviderError("Google did not grant offline access. Remove this app's connection in Google and reconnect.")
        elif platform == "instagram":
            short = checked(self.client.post("https://api.instagram.com/oauth/access_token", data=payload))
            # Instagram Login may wrap the code exchange in a one-element data array.
            if isinstance(short.get("data"), list) and short["data"]:
                short = short["data"][0]
            token = checked(self.client.get("https://graph.instagram.com/access_token", params={
                "grant_type": "ig_exchange_token", "client_secret": payload["client_secret"], "access_token": short["access_token"]}))
            identity = checked(self.client.get("https://graph.instagram.com/me", params={"fields": "user_id,username"},
                                               headers={"Authorization": "Bearer " + token["access_token"]}))
            token.update(account_id=str(identity.get("user_id") or identity.get("id") or short["user_id"]), name=identity["username"])
        else:
            payload["client_key"] = payload.pop("client_id")
            token = checked(self.client.post(TIKTOK + "/oauth/token/", data=payload))
            identity = checked(self.client.get(TIKTOK + "/user/info/", params={"fields": "open_id,display_name"},
                                               headers={"Authorization": "Bearer " + token["access_token"]}), tiktok=True)
            token.update(account_id=token["open_id"], name=identity["user"]["display_name"], mode=c.get("tiktok_mode", "draft"))
        token["expires_at"] = time.time() + int(token.get("expires_in", 3600))
        self.store.set("token_" + platform, token)

    def token(self, platform, expected_account=None):
        with self.token_lock:
            token = self.store.get("token_" + platform)
            if not token:
                raise ProviderError("Connect this account in Connections first.")
            if expected_account and token["account_id"] != expected_account:
                raise ProviderError("The connected account changed. This post belongs to the previous account.")
            # Refresh Instagram well before expiry, since expired long-lived tokens cannot refresh.
            margin = 86400 * 7 if platform == "instagram" else 300
            if token["expires_at"] > time.time() + margin:
                return token
            c = self.config()
            if platform == "instagram":
                refreshed = checked(self.client.get("https://graph.instagram.com/refresh_access_token", params={
                    "grant_type": "ig_refresh_token", "access_token": token["access_token"]}))
            else:
                if not token.get("refresh_token"):
                    raise ProviderError("Account access expired. Reconnect in Connections.")
                payload = {"client_id": c[platform + "_client_id"], "client_secret": c[platform + "_client_secret"],
                           "grant_type": "refresh_token", "refresh_token": token["refresh_token"]}
                endpoint = GOOGLE_TOKEN
                if platform == "tiktok":
                    payload["client_key"] = payload.pop("client_id")
                    endpoint = TIKTOK + "/oauth/token/"
                refreshed = checked(self.client.post(endpoint, data=payload))
            token.update(refreshed)
            token["expires_at"] = time.time() + int(refreshed.get("expires_in", 3600))
            self.store.set("token_" + platform, token)
            return token

    def creator(self):
        token = self.token("tiktok")
        if token.get("mode") != "direct":
            return {"mode": "draft", "creator_nickname": token["name"]}
        info = checked(self.client.post(TIKTOK + "/post/publish/creator_info/query/",
                       headers={"Authorization": "Bearer " + token["access_token"]}, json={}), tiktok=True)
        return {**info, "mode": "direct"}

    def start(self, job, target):
        p = target["platform"]
        token = self.token(p, target["account"])
        headers = {"Authorization": "Bearer " + token["access_token"]}
        options = job["options"]
        update = lambda **kw: self.store.update_target(job["id"], p, **kw)
        if p == "youtube":
            payload = {"snippet": {"title": options["youtube_title"], "description": job["caption"], "categoryId": "22"},
                       "status": {"privacyStatus": options["youtube_privacy"], "selfDeclaredMadeForKids": options["made_for_kids"],
                                  "containsSyntheticMedia": options["ai_generated"]}}
            response = self.client.post("https://www.googleapis.com/upload/youtube/v3/videos",
                params={"uploadType": "resumable", "part": "snippet,status"}, json=payload,
                headers={**headers, "X-Upload-Content-Length": str(job["size"]), "X-Upload-Content-Type": "video/mp4"})
            if response.is_error:
                checked(response)
            url = validated_upload_url(response.headers.get("location", ""), ["googleapis.com"])
            # Session URL is a credential. Persist encrypted, never return it in job JSON.
            self.store.set("upload_" + job["id"] + "_youtube", url)
            result = checked(self.client.put(url, content=file_chunks(job["path"]),
                headers={**headers, "Content-Type": "video/mp4", "Content-Length": str(job["size"])}))
            update(status="processing", remote_id=result["id"], result={"url": "https://www.youtube.com/shorts/" + result["id"],
                   "privacy": result.get("status", {}).get("privacyStatus", "unknown")}, next_check=time.time() + 10)
        elif p == "instagram":
            base = "https://graph.instagram.com/" + self.config().get("instagram_version", "v24.0")
            media_url = self.media_url(job["id"])
            result = checked(self.client.post(base + "/" + token["account_id"] + "/media", headers=headers,
                             data={"media_type": "REELS", "video_url": media_url, "caption": job["caption"], "share_to_feed": "true"}))
            update(status="processing", remote_id=result["id"], next_check=time.time() + 10)
        else:
            chunk, count = tiktok_chunks(job["size"])
            payload = {"source_info": {"source": "FILE_UPLOAD", "video_size": job["size"], "chunk_size": chunk, "total_chunk_count": count}}
            direct = token.get("mode") == "direct"
            if options["tiktok_mode"] != token.get("mode", "draft"):
                raise ProviderError("TikTok posting mode changed. Create a new post after checking Connections.")
            if direct:
                creator = self.creator()
                if options["tiktok_privacy"] not in creator.get("privacy_level_options", []):
                    raise ProviderError("TikTok privacy options changed. Select a currently available privacy setting.")
                if job["meta"]["duration"] > creator["max_video_post_duration_sec"]:
                    raise ProviderError("This video exceeds the connected TikTok account's duration limit.")
                payload["post_info"] = {"title": job["caption"], "privacy_level": options["tiktok_privacy"],
                    "disable_comment": creator.get("comment_disabled", False) or not options["allow_comment"],
                    "disable_duet": creator.get("duet_disabled", False) or not options["allow_duet"],
                    "disable_stitch": creator.get("stitch_disabled", False) or not options["allow_stitch"],
                    "brand_content_toggle": options["branded_content"], "brand_organic_toggle": options["own_brand"],
                    "is_aigc": options["ai_generated"]}
            endpoint = "/post/publish/video/init/" if direct else "/post/publish/inbox/video/init/"
            result = checked(self.client.post(TIKTOK + endpoint, headers=headers, json=payload), tiktok=True)
            update(remote_id=result["publish_id"])
            url = validated_upload_url(result["upload_url"], ["tiktokapis.com"])
            for index in range(count):
                offset = index * chunk
                length = job["size"] - offset if index == count - 1 else chunk
                response = self.client.put(url, content=file_chunks(job["path"], offset, length), headers={
                    "Content-Type": "video/mp4", "Content-Length": str(length),
                    "Content-Range": f"bytes {offset}-{offset + length - 1}/{job['size']}"})
                if response.status_code not in (201, 206):
                    raise ProviderError(f"TikTok file transfer failed (HTTP {response.status_code}). Check status before posting again.")
            update(status="processing", next_check=time.time() + 10)

    def media_url(self, job_id):
        from .security import media_signature
        base = self.config()["base_url"].rstrip("/")
        if urlparse(base).scheme != "https" or urlparse(base).hostname in ("localhost", "127.0.0.1", "::1"):
            raise ProviderError("Instagram needs a public HTTPS URL to fetch the video. Set it in Connections and use your HTTPS proxy or tunnel.")
        expires = int(time.time()) + 86400
        return f"{base}/media/{job_id}?expires={expires}&signature={media_signature(self.store, job_id, expires)}"

    def poll(self, job, target):
        p, remote = target["platform"], target["remote_id"]
        if not remote:
            raise ProviderError("No remote ID was received. Check the platform manually before uploading again.")
        token = self.token(p, target["account"])
        headers = {"Authorization": "Bearer " + token["access_token"]}
        update = lambda **kw: self.store.update_target(job["id"], p, **kw)
        update(next_check=time.time() + 20)
        if p == "tiktok":
            result = checked(self.client.post(TIKTOK + "/post/publish/status/fetch/", headers=headers, json={"publish_id": remote}), tiktok=True)
            status = result.get("status")
            if status == "PUBLISH_COMPLETE":
                public_ids = result.get("publicaly_available_post_id", [])
                # Display name is not the account username; use TikTok's stable post redirect.
                url = "https://www.tiktok.com/@_/video/" + str(public_ids[0]) if public_ids else ""
                privacy = job["options"].get("tiktok_privacy")
                update(status="published" if privacy == "PUBLIC_TO_EVERYONE" else "restricted", error="",
                       result={"url": url, "privacy": privacy, "message": "TikTok finished publishing. Post visibility follows your selection and app restrictions."})
            elif status == "SEND_TO_USER_INBOX":
                update(status="draft", error="", result={"message": "Open TikTok inbox to finish posting. TikTok's draft API does not attach the caption; copy it here."})
            elif status == "FAILED":
                update(status="failed", error=str(result.get("fail_reason", "TikTok rejected this upload.")))
            elif status not in ("PROCESSING_UPLOAD", "PROCESSING_DOWNLOAD", "PUBLISHING"):
                update(status="attention", error="TikTok returned an unrecognized status. Check your account.")
            else:
                update(status="processing", error="")
        elif p == "youtube":
            result = checked(self.client.get("https://www.googleapis.com/youtube/v3/videos", headers=headers,
                             params={"part": "status,processingDetails", "id": remote}))
            if not result.get("items"):
                raise ProviderError("YouTube has not returned this video. Check YouTube Studio before uploading again.")
            video = result["items"][0]
            status = video.get("status", {})
            processing = video.get("processingDetails", {}).get("processingStatus")
            if status.get("uploadStatus") in ("failed", "rejected", "deleted") or processing in ("failed", "terminated"):
                update(status="failed", error=str(status.get("rejectionReason") or status.get("failureReason") or "YouTube processing failed."))
            elif processing == "succeeded" or status.get("uploadStatus") == "processed":
                privacy = status.get("privacyStatus", "unknown")
                update(status="published" if privacy == "public" else "restricted", error="", result={
                    "url": "https://www.youtube.com/shorts/" + remote, "privacy": privacy,
                    "message": f"Uploaded to YouTube ({privacy}). YouTube decides Shorts classification. Unverified API projects are restricted to private uploads."})
            else:
                update(status="processing", error="")
        else:
            base = "https://graph.instagram.com/" + self.config().get("instagram_version", "v24.0")
            result = checked(self.client.get(base + "/" + remote, headers=headers, params={"fields": "status_code,status"}))
            status = result.get("status_code")
            if status == "FINISHED":
                # Persist before a possibly ambiguous publish call. Never re-publish this container automatically.
                update(status="publishing")
                result = checked(self.client.post(base + "/" + token["account_id"] + "/media_publish", headers=headers,
                                                 data={"creation_id": remote}))
                media_id = result["id"]
                # Publishing succeeded even when the optional permalink lookup fails.
                update(status="published", error="", result={"media_id": media_id, "message": "Your Instagram Reel was published."})
                try:
                    link = checked(self.client.get(base + "/" + media_id, headers=headers, params={"fields": "permalink"}))
                    update(result={"media_id": media_id, "url": link.get("permalink", ""), "message": "Your Instagram Reel was published."})
                except (ProviderError, httpx.HTTPError):
                    pass
            elif status == "PUBLISHED":
                update(status="published", error="", result={"message": "Instagram confirmed this container has been published."})
            elif status in ("ERROR", "EXPIRED"):
                update(status="failed", error=str(result.get("status") or "Instagram rejected or expired the video container.")[:700])
            elif status == "IN_PROGRESS":
                update(status="processing", error="")
            else:
                update(status="attention", error="Instagram returned an unrecognized processing status. Check the account.")

    def check_only(self, job, target):
        # An ambiguous Instagram publish must only be inspected, never repeated.
        if target["platform"] == "instagram" and target["status"] == "attention":
            token = self.token("instagram", target["account"])
            base = "https://graph.instagram.com/" + self.config().get("instagram_version", "v24.0")
            result = checked(self.client.get(base + "/" + target["remote_id"], params={"fields": "status_code,status"},
                headers={"Authorization": "Bearer " + token["access_token"]}))
            if result.get("status_code") == "PUBLISHED":
                self.store.update_target(job["id"], "instagram", status="published", error="", result={"message": "Instagram confirmed the Reel was published."})
            else:
                self.store.update_target(job["id"], "instagram", error="Instagram container status: " + str(result.get("status_code")) + ". Check the account before posting again.")
        else:
            self.poll(job, target)


class Publisher:
    def __init__(self, store, providers):
        self.store, self.providers = store, providers
        self.stop = threading.Event()
        self.operation_lock = threading.Lock()
        self.thread = threading.Thread(target=self.run, daemon=True, name="relay-publisher")
        self.last_refresh = 0

    def run(self):
        while not self.stop.is_set():
            with self.operation_lock:
                if time.time() - self.last_refresh > 3600:
                    self.last_refresh = time.time()
                    for platform in PLATFORMS:
                        if self.store.get("token_" + platform):
                            try:
                                self.providers.token(platform)
                            except (ProviderError, httpx.HTTPError, KeyError, ValueError):
                                # Reconnecting is explicit; never discard saved tokens on a transient error.
                                pass
                rows = self.store.rows("SELECT * FROM targets WHERE status='queued' OR (status='processing' AND next_check<=?) ORDER BY next_check LIMIT 1", (time.time(),))
                if rows:
                    self.handle(rows[0])
            self.stop.wait(1 if rows else 3)

    def handle(self, target):
        job = self.store.job(target["job_id"])
        try:
            if target["status"] == "queued":
                self.store.update_target(job["id"], target["platform"], status="uploading")
                self.providers.start(job, target)
            else:
                self.providers.poll(job, target)
        except (ProviderError, httpx.HTTPError, KeyError, ValueError, OSError) as exc:
            latest = self.store.rows("SELECT * FROM targets WHERE job_id=? AND platform=?", (job["id"], target["platform"]))[0]
            if latest["status"] in ("published", "restricted", "draft"):
                return
            message = str(exc) if isinstance(exc, ProviderError) else "The platform request could not be completed. Check status and account access before posting again."
            # Any failed write could have succeeded remotely; do not offer automatic retry.
            self.store.update_target(job["id"], target["platform"], status="attention", error=message, next_check=time.time() + 60)

    def close(self):
        self.stop.set()
        self.thread.join(timeout=25)
        if not self.thread.is_alive():
            self.providers.client.close()
            self.store.close()
