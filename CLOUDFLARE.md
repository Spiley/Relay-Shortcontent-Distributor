# Cloudflare Tunnel on Ubuntu

Keep Relay as one Docker container and install cloudflared directly on Ubuntu as a system service. It connects to Relay at `http://127.0.0.1:8088` and provides a public HTTPS URL. You do not need to forward a port on your router.

A stable address such as `https://relay.example.com` needs a domain added to Cloudflare with its nameservers configured. Cloudflare Tunnel can use the free plan; domain registration is a separate cost. If you have no domain, see the temporary tunnel option below.

## 1. Start Relay and set its password

Copy the updated project files to your Ubuntu project folder, including `compose.yaml` and `Dockerfile`. Run these commands from that folder:

```sh
docker compose up -d --build
curl --fail http://127.0.0.1:8088/health
```

Open `http://192.168.1.10:8088`, set your password if needed, and sign in. Keep LAN access enabled until you save the public Website URL in step 4.

## 2. Install cloudflared on Ubuntu

These commands install the official Cloudflare package on the Ubuntu host. They can run from any folder.

```sh
sudo apt-get update
sudo apt-get install -y curl ca-certificates
sudo mkdir -p --mode=0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt-get update
sudo apt-get install -y cloudflared
```

## 3. Create the tunnel and route

1. Open the [Cloudflare dashboard](https://dash.cloudflare.com), select your account, and go to **Networking > Tunnels**. Older dashboard layouts may show tunnels under **Zero Trust > Networks > Connectors**.
2. Choose **Create a tunnel**, choose **Cloudflared** if prompted, and name it `relay`.
3. Select the Linux/Debian instructions for your Ubuntu server's architecture. Since cloudflared is already installed, run the displayed **service install** command on Ubuntu. It looks like `sudo cloudflared service install YOUR_TUNNEL_TOKEN`. Use the actual command from your dashboard.
4. Wait for the connector to appear connected, then continue.
5. Add a **Published application** route under the tunnel's **Routes** tab (called **Public Hostname** in some dashboard layouts):

   | Field | Value |
   | --- | --- |
   | Subdomain | `relay` |
   | Domain | Your domain, for example `example.com` |
   | Path | Leave empty |
   | Service type | `HTTP` |
   | Service URL/address | `127.0.0.1:8088` |

   If the interface has one combined Service URL field, enter `http://127.0.0.1:8088`.

6. Save. Leave the origin HTTP Host Header override empty so Relay receives the public hostname. Cloudflare provides HTTPS to visitors; the connection to Relay on the same server uses HTTP.

Cloudflared's service keeps running after you disconnect SSH and starts again after reboot. No tunnel token needs to go into Relay or its `.env`.

## 4. Save the public URL in Relay

Before opening the public address, open Relay using `http://192.168.1.10:8088` and go to **Connections**. Set **Website URL** to your actual address, for example `https://relay.example.com`, and save. Relay validates incoming hostnames, so this step prevents a `400` when opening the tunnel URL.

Now edit `.env` in the project folder. If it does not exist, first run `cp .env.example .env`. Keep any other settings and set these values:

```dotenv
PORT=8088
BIND_ADDRESS=127.0.0.1
FORWARDED_ALLOW_IPS=*
MAX_UPLOAD_MB=90
```

`FORWARDED_ALLOW_IPS=*` lets Uvicorn recognize Cloudflare's HTTPS headers across Docker's bridge. Use it with the localhost-only binding shown here. This disables direct LAN access; use the tunnel URL going forward. For a setup that also needs LAN access, keep `BIND_ADDRESS=0.0.0.0` and trust specific proxy addresses instead of `*`.

Cloudflare Free allows request uploads up to 100 MB. Relay's 90 MB setting leaves room for multipart overhead. The tunnel does not support this project's default 512 MB upload size in one request on the free plan. Uploads can also time out during lengthy transcoding; use H.264/AAC MP4 when possible. A 524 after upload is not proof the job failed: check Relay's History before submitting a new post.

Apply the settings:

```sh
docker compose up -d --build --force-recreate
```

Open `https://relay.example.com` using your actual domain and sign in again. Set the exact callback addresses in your developer apps, using the copy buttons in Connections:

```text
https://relay.example.com/oauth/youtube/callback
https://relay.example.com/oauth/instagram/callback
https://relay.example.com/oauth/tiktok/callback
```

Connect accounts from the HTTPS website so each callback returns to the same browser session. The tunnel provides reachability; the platforms still require their account permissions.

Use Relay's password protection. Instagram must fetch signed `/media/*` links without an interactive Cloudflare Access login or browser challenge. If you already use Access or challenge rules, exempt the signed media route; Relay still checks the signature. Keep `/oauth/*` accessible to the returning browser. Bypass caching for this application's hostname if you have custom rules that cache dynamic pages or media.

## Temporary tunnel without a domain

After steps 1 and 2, run:

```sh
cloudflared tunnel --url http://127.0.0.1:8088
```

Keep that terminal running. Cloudflared prints a random `https://something.trycloudflare.com` address. Through the LAN website, save that exact address as Relay's Website URL. Then apply step 4's `.env` settings and recreate Relay. Open the temporary HTTPS address and sign in.

The hostname changes each time you start a new quick tunnel. You must update Relay's Website URL and any registered OAuth callbacks each time. This is useful for testing; a named tunnel on your domain is the stable setup for account connections. Do not use the quick tunnel's email login restriction for Instagram media fetching.

## Check status

```sh
docker compose ps
curl --fail http://127.0.0.1:8088/health
sudo systemctl status cloudflared --no-pager
sudo journalctl -u cloudflared -n 50 --no-pager
```

- `502`: check Relay is running and the tunnel's origin is `http://127.0.0.1:8088`, not port 8000 or HTTPS.
- `400` from Relay: save the exact public Website URL through a local session. With localhost-only binding, temporarily forward it from your computer using `ssh -L 8088:127.0.0.1:8088 USER@192.168.1.10`, then open `http://localhost:8088`.
- `413`: the upload exceeded Cloudflare's request limit or a lower limit configured for your zone.
- Tunnel offline: inspect the cloudflared service and check outbound connectivity to Cloudflare on port 7844.

References: [Cloudflare dashboard tunnel setup](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/get-started/create-remote-tunnel/), [Ubuntu package installation](https://developers.cloudflare.com/tunnel/features/locally-managed-tunnels/create-local-tunnel/), [Quick Tunnels](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/), [upload limits](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/4xx-client-error/error-413/).
