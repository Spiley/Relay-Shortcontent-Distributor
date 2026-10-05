# Relay

A self-hosted video uploader for TikTok, Instagram Reels, and YouTube Shorts. One Docker Compose service runs the website, posting worker, SQLite database, and FFmpeg. No Redis, separate database container, or paid publishing service.

## Run

```sh
docker compose up -d --build
```

Open **http://localhost:8088**, choose a Relay password, and start uploading. The default **Prepare for manual upload** mode works immediately without platform credentials: it prepares your video, saves it locally, and gives you download, caption-copy, and platform-upload links.

When Docker runs on another machine, open **http://YOUR-SERVER-IP:8088** instead, for example `http://192.168.1.10:8088`. Compose now binds to all host interfaces by default. If an existing `.env` sets `BIND_ADDRESS=127.0.0.1`, change it to `BIND_ADDRESS=0.0.0.0` and recreate the container. Relay accepts local-network IP addresses; your password still protects the workspace. HTTP LAN access also supports manual uploads. Connecting accounts from a remote server needs the appropriate HTTPS or localhost callback setup described below.

For automatic posting, open **Connections**. It contains step-by-step setup instructions, callback URLs with copy buttons, credential fields, and account connection buttons. Connect the accounts you need, disable manual mode, select your destinations, and upload a video with its caption. The worker continues after you close the tab. History shows each platform's actual outcome.

## What free automatic posting requires

The software is free. Official platform APIs still require a developer app and permission from your account. There is no supported zero-auth method for automatically publishing publicly to all three accounts.

| Destination | Setup | Limits to understand |
| --- | --- | --- |
| YouTube Shorts | Google Cloud project, YouTube Data API enabled, Web application OAuth client, one-time channel connection | Google restricts uploads from unverified API projects to private. Testing-mode refresh tokens can expire after 7 days. Square or vertical videos up to 3 minutes are eligible for Shorts; YouTube makes the final classification. |
| Instagram Reels | Meta app with **Instagram Login**, Instagram app ID/secret, Creator or Business account, tester invitation or approved access | Instagram must be able to fetch the video from a public HTTPS URL. No Facebook Page is required for the Instagram Login flow used here. |
| TikTok | TikTok developer app with Login Kit and Content Posting API, relevant approved scopes, HTTPS callback | Draft mode needs `video.upload`; you finish the post in TikTok and paste the caption. Direct mode needs `video.publish`. Unaudited direct posts require private accounts and `SELF_ONLY`. Public direct posting needs an audit; TikTok explicitly excludes utilities only for your own/team accounts. This app cannot promise approval. |

Official references: [YouTube upload restrictions](https://developers.google.com/youtube/v3/docs/videos/insert), [Google OAuth token expiration](https://developers.google.com/identity/protocols/oauth2#expiration), [Shorts eligibility](https://support.google.com/youtube/answer/15424877), [Instagram Login](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/), [Instagram publishing](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/content-publishing/), [TikTok direct posting and intended use](https://developers.tiktok.com/docs/en/content-sharing-guidelines), [TikTok draft uploads](https://developers.tiktok.com/docs/en/content-posting-api-reference-upload-video).

## Connect your accounts

You can configure one destination at a time. Unconnected destinations remain disabled in automatic mode.

**YouTube:** Create a project in Google Cloud and enable YouTube Data API v3. Configure Google Auth Platform's consent screen; add your Google account as a test user while in Testing. Create a **Web application** OAuth client and register the exact callback shown in Relay, initially `http://localhost:8088/oauth/youtube/callback`. Enter the client ID/secret and connect your channel. Relay requests upload and read-only channel permissions to identify the destination and check processing. A requested Public upload may still be private because of Google's app restrictions; Relay displays the returned visibility.

**Instagram:** Create a Meta developer app with **Instagram API with Instagram Login**. Use the Instagram app ID and secret, rather than credentials for Facebook Login. Configure your public HTTPS redirect URI, `https://YOUR-HOST/oauth/instagram/callback`, and the `instagram_business_basic` and `instagram_business_content_publish` permissions. Add your professional account as an Instagram tester, accept the invitation, save, and connect. Media is served through a signed link valid for 24 hours; your password-protected API and videos are otherwise private. Relay does not use the Facebook Login-only resumable media endpoint.

**TikTok:** Add Login Kit and Content Posting API to your app. Set `https://YOUR-HOST/oauth/tiktok/callback`. Draft mode requests `user.info.basic,video.upload`; direct mode requests `user.info.basic,video.publish`. The website displays current creator privacy choices, interaction permissions, and posting consent for direct mode. Switching mode clears Relay's TikTok token and requires reconnecting with the new scope. Draft uploads do not carry a caption; copy it from history and paste it when finishing in the TikTok inbox.

## Public HTTPS and Docker

For an Ubuntu server with Cloudflare Tunnel, follow [the Cloudflare setup guide](CLOUDFLARE.md). Cloudflared runs as an Ubuntu service; Relay remains one Docker container. Compose passes the `FORWARDED_ALLOW_IPS` setting from `.env` to Uvicorn for HTTPS proxy headers.

YouTube works from localhost. Instagram needs externally reachable HTTPS to fetch media; TikTok needs an HTTPS OAuth redirect. Use an existing HTTPS reverse proxy or a tunnel pointed at Relay. These are infrastructure requirements, not extra services in this Compose file.

1. Set up Relay and its password using localhost or your server's LAN IP first.
2. Copy `.env.example` to `.env` if you need different port or binding settings.
3. The default `BIND_ADDRESS=0.0.0.0` supports LAN access and a reverse proxy on a different machine. For a tunnel running on the Docker host, you may restrict the binding to `127.0.0.1`. Use HTTPS for the public URL.
4. Run `docker compose up -d` to apply port/environment changes.
5. In **Connections**, set **Website URL** to the public origin, e.g. `https://relay.example.com` (no path). Save.
6. Register the displayed callback URLs with each developer app. Open Relay on the public HTTPS URL and reconnect there so the OAuth callback uses the same browser session.

Forward the original `Host` header and `X-Forwarded-Proto`. If your proxy's trusted address is not localhost, configure Uvicorn's `--forwarded-allow-ips` for that proxy, while restricting direct access. The app validates its configured public host and request origins. Do not add a password gate to `/media/*` in the proxy: Instagram must fetch valid signed URLs without your Relay login. Leave Relay's signature validation in place. Turn off proxy access logging of query strings for `/media/*` and `/oauth/*` to avoid logging signed links and OAuth codes.

Configure your proxy/tunnel to accept at least the upload limit plus multipart overhead and allow long preparation requests (up to 30 minutes for transcoding). HTTP LAN URLs work for accessing your workspace and manual uploads. YouTube OAuth cannot redirect to a private LAN IP: use a public HTTPS URL or localhost port forwarding from your browser's computer to the server. TikTok and Instagram connections use public HTTPS.

## Terms and Privacy Policy

Public policy pages are included at `/terms` and `/privacy`, with links on the login screen, in Connections, and in the workspace footer. They are available without signing in on the configured Website URL. Use `https://YOUR-HOST/terms` for TikTok's Terms of Service URL and `https://YOUR-HOST/privacy` for its Privacy Policy URL.

The text identifies **Spiley** as the operator and **spileyapps@gmail.com** as the contact, as supplied for this installation. Read and review the pages before submitting them. Update `app/static/terms.html` and `app/static/privacy.html` if operator details, applicable requirements, hosting, backup retention, or the service's behavior change. Rebuild with `docker compose up -d --build` after editing. These pages do not grant platform permissions or guarantee app review approval.

## Storage and behavior

- The named `relay-data` volume stores videos, SQLite records, and a generated encryption key. Credentials and tokens are encrypted using that key; protect and back up the whole volume together. Removing it erases your videos, connections, and password.
- The default upload limit is **512 MB**, configurable via `MAX_UPLOAD_MB`. Suitable H.264/AAC videos are remuxed; other supported videos are converted to H.264 MP4 with FFmpeg. Videos are previewed locally before upload. Relay does not add watermarks or crop them.
- For all three destinations, use square or portrait video, 360–4096 pixels per dimension, 3 seconds to 3 minutes. The server checks Shorts eligibility and normalizes codec/frame rate. Platform-specific checks can still reject a video.
- The caption limit is 2,200 UTF-16 characters to suit TikTok and Instagram. YouTube gets the full caption as its description and the first line as a title (max 100 characters), unless you specify a Shorts title.
- Queued jobs survive restarts. Jobs interrupted during a remote write become **Needs attention** instead of being reposted automatically. Use **Check status** where a remote identifier is available. Ambiguous Instagram publish calls are inspected without repeating the publish request. After inspecting the account yourself, **I've checked this on the platform** records your manual review and allows removal of the local file; it does not claim that Relay verified publication.
- Failed destinations do not block the other destinations. A browser retry with the same upload request ID returns the existing saved post. Repeatedly uploading the same file as a **new** post is an intentional new upload.
- Tokens refresh on use and are maintained hourly while the worker runs. Revoked, expired, or testing-only grants may need reconnecting. Disconnect removes Relay's saved connection; revoke the developer app in the platform account settings if you want to remove provider authorization too. It does not cancel jobs already accepted remotely.
- Completed local videos can be removed from history. This does not delete posts on the platforms. Keep pending and uncertain files until you resolve their status.
- Run one app process (`--workers 1`) and one container against the volume. The internal queue is intended for one person's workspace, not a multi-tenant service.

## Maintenance

```sh
# Inspect status and server logs (request access logs are disabled)
docker compose ps
docker compose logs -f relay

# Update after editing/pulling the source
docker compose up -d --build

# Stop without removing the data volume
docker compose down

# Recover a forgotten Relay password; prompts securely and revokes old sessions
docker compose exec relay python -m app.admin
```

## Development and verification

Use Python 3.14, install `requirements-dev.txt`, and make `ffmpeg` and `ffprobe` available on PATH:

```sh
python -m venv .venv
# Activate your virtual environment, then:
pip install -r requirements-dev.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8088 --workers 1 --no-access-log
python -m pytest -q tests
docker compose config --quiet
```

Tests cover login/access controls, cross-origin rejection, signed media, encrypted credentials, OAuth state replay, idempotent uploads, TikTok chunk boundaries and consent, official adapter request/response flows, private YouTube outcomes, token refresh, and avoiding duplicate posts after interrupted writes. The media test exercises real FFmpeg conversion when FFmpeg is available. Mocked platform tests do not establish that your developer apps are approved; live publishing requires your own credentials and connected accounts.
