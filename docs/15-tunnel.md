# Cloudflare Tunnel (Remote Access)

> Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
> Licensed under the [MIT License](../LICENSE).

Cloudflare Tunnel creates an outbound-only encrypted connection from
the GlowUp coordinator to Cloudflare's edge network.  The phone
connects to `https://lights.yourdomain.com` — no ports are opened on
the router and no dynamic DNS is needed.

## Prerequisites

- A domain managed by Cloudflare DNS (free plan is sufficient).
- The GlowUp server running on the Pi (see [REST API Server](11-rest-api.md)).
- `cloudflared` installed on the Pi.

## Install cloudflared

```bash
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64.deb -o /tmp/cloudflared.deb
sudo dpkg -i /tmp/cloudflared.deb
```

## Authenticate

```bash
cloudflared tunnel login
```

This opens a browser for Cloudflare login.  Select the domain you
want to use.  A certificate is saved to `~/.cloudflared/`.

## Create the Tunnel

```bash
cloudflared tunnel create lifx
```

Note the tunnel UUID printed — you'll need it for the config.

## Configure

Create `~/.cloudflared/config.yml`:

```yaml
tunnel: <TUNNEL-UUID>
credentials-file: /home/pi/.cloudflared/<TUNNEL-UUID>.json

ingress:
  - hostname: lights.yourdomain.com
    service: http://localhost:8420
  - service: http_status:404
```

Replace `<TUNNEL-UUID>` with the UUID from the create step, and
`lights.yourdomain.com` with your actual subdomain.

## Add DNS Route

```bash
cloudflared tunnel route dns lifx lights.yourdomain.com
```

This creates a CNAME record pointing your subdomain to the tunnel.

## Run the Tunnel

Test it manually first:

```bash
cloudflared tunnel run lifx
```

Then access `https://lights.yourdomain.com/api/devices` from
anywhere.  The bearer token from `~/.glowup_token` is still
required for API access.

## Make It Persistent (systemd)

```bash
sudo cloudflared service install
sudo systemctl enable cloudflared
sudo systemctl start cloudflared
```

The tunnel will start automatically on boot and reconnect if the
network drops.

## iOS App Configuration

In the GlowUp iOS app, set the server URL to
`https://lights.yourdomain.com` (Settings → Server URL).  The app
works identically over the tunnel — all API calls pass through
Cloudflare's edge.

## Security Notes

- The tunnel is outbound-only — no inbound ports are opened.
- All traffic is encrypted (TLS) between the phone, Cloudflare, and
  the Pi.
- The bearer token is still required — the tunnel doesn't bypass
  authentication.
- Cloudflare's free plan includes unlimited tunnel bandwidth.
