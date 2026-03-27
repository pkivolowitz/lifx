# Cloudflare Tunnel Setup

Cloudflare Tunnel creates an outbound-only encrypted connection from
your Pi to Cloudflare's edge network.  Your phone connects to
`https://lights.yourdomain.com` — Cloudflare handles TLS and routes
traffic through the tunnel to your local GlowUp server.

**No ports are opened on your router.  No dynamic DNS is needed.**

## Prerequisites

- A domain with DNS managed by Cloudflare (free tier works).  If your
  domain is registered elsewhere, change its nameservers to Cloudflare
  (Cloudflare walks you through this — it's a one-time change at your
  registrar, no domain transfer required).
- A Cloudflare account (free).
- The GlowUp server running on the Pi (`server.py`).

## 1. Install cloudflared on the Pi

```bash
# ARM64 (Raspberry Pi 4/5 with 64-bit OS)
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 \
    -o /usr/local/bin/cloudflared
chmod +x /usr/local/bin/cloudflared

# Verify
cloudflared --version
```

## 2. Authenticate with Cloudflare

```bash
cloudflared tunnel login
```

This opens a browser on your desktop (or prints a URL you can visit).
Select the domain you want to use and authorize access.  A certificate
is saved to `~/.cloudflared/cert.pem`.

## 3. Create a named tunnel

```bash
cloudflared tunnel create lifx
```

Note the tunnel UUID printed — you'll need it for the config file.
Credentials are saved to `~/.cloudflared/<UUID>.json`.

## 4. Configure the tunnel

Create `/etc/cloudflared/config.yml`:

```yaml
tunnel: <tunnel-uuid>
credentials-file: /home/pi/.cloudflared/<tunnel-uuid>.json

ingress:
  - hostname: lights.yourdomain.com
    service: http://localhost:8420
  - service: http_status:404
```

The `ingress` section maps your chosen hostname to the local GlowUp
server.  The catch-all rule at the bottom returns 404 for any other
hostname (required by cloudflared).

## 5. Create a DNS record

```bash
cloudflared tunnel route dns lifx lights.yourdomain.com
```

This creates a CNAME record pointing `lights.yourdomain.com` to your
tunnel.  Cloudflare handles TLS certificates automatically.

## 6. Test the tunnel manually

```bash
cloudflared tunnel run lifx
```

From another machine (or your phone on cellular), test:

```bash
curl -H "Authorization: Bearer YOUR_TOKEN" \
    https://lights.yourdomain.com/api/devices
```

If you get a JSON response listing your devices, the tunnel works.

## 7. Install as a systemd service

```bash
sudo cloudflared service install
sudo systemctl enable cloudflared
sudo systemctl start cloudflared
```

The tunnel now starts automatically on boot, alongside the GlowUp
server (`glowup-server.service`).

## Security layers

1. **Outbound-only tunnel** — no ports opened on your router, no port
   forwarding rules.  The Pi initiates the connection to Cloudflare.
2. **TLS termination** — Cloudflare provides HTTPS with a valid
   certificate.  Traffic between your phone and Cloudflare's edge is
   encrypted.
3. **Bearer token** — every API request requires a valid token in the
   `Authorization` header.  The token is checked by the GlowUp server
   with timing-safe comparison.
4. **Optional: Cloudflare Access** — for an additional zero-trust layer,
   create a Cloudflare Access application (see below).

## Optional: Cloudflare Access (zero-trust)

Cloudflare Access adds identity verification *before* traffic reaches
your tunnel.  Even if someone guesses your API endpoint, they can't
reach it without passing Cloudflare's authentication.

1. In the Cloudflare dashboard: **Zero Trust → Access → Applications**.
2. Add a self-hosted application for `lights.yourdomain.com`.
3. Create a policy: **Allow** with **Email** containing your email.
4. When accessing the API from a browser, Cloudflare prompts for a
   one-time code sent to your email.

For the iOS app, use a **Service Token** instead of email OTP:
1. **Zero Trust → Access → Service Auth → Service Tokens**.
2. Create a token — note the Client ID and Client Secret.
3. Add a second policy: **Allow** with **Service Token** matching the
   token you created.
4. The iOS app sends `CF-Access-Client-Id` and `CF-Access-Client-Secret`
   headers alongside the Bearer token.

## Troubleshooting

- **Tunnel not connecting**: Check `sudo systemctl status cloudflared`
  and `sudo journalctl -u cloudflared -f` for errors.
- **DNS not resolving**: Verify the CNAME exists with
  `dig lights.yourdomain.com` — it should point to `<UUID>.cfargotunnel.com`.
- **502 Bad Gateway**: The GlowUp server isn't running on port 8420.
  Check `sudo systemctl status glowup-server`.
- **SSE stream buffered**: The `X-Accel-Buffering: no` header in
  `server.py` tells Cloudflare not to buffer the stream.  If live
  colors lag, verify this header is present in responses.
