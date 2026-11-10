# Deploying EPUB Purifier

EPUB Purifier is a Python 3.10+ FastAPI/uvicorn application that serves both the
web UI and the JSON API from one process. The supported deployment target is a
Linux VPS where you control that process: either Docker or a systemd service,
with Caddy in front for TLS and the upload body-size limit.

The default listen address is `127.0.0.1:8000`. The Docker image overrides the
address to `0.0.0.0` inside the container; the published host port stays bound
to loopback so Caddy is the only public entry point.

## Why the Hostinger managed Node.js Web App cannot run this

The Hostinger panel that asks for a framework preset, Node version and entry
file is a **Node.js Web App** builder. It runs Node.js and requires an entry file
ending in `.js`, `.mjs` or `.cjs`. This repository is Python:

- The app is created by `epub_purifier.web.app:create_app` and started by the
  Python console script `epub-purifier-web` (`epub_purifier.web.app:main`)
  declared in `pyproject.toml`.
- `package.json` exists only for the Playwright UI test. Its `start` script calls
  `epub-purifier-web`, which is a Python entry point, not a Node file.
- The `web` extra installs FastAPI, uvicorn and python-multipart; there is no
  Node server or Node dependency to start.
- Hostinger documents Python as supported exclusively on
  [VPS hosting](https://www.hostinger.com/support/which-programming-languages-and-frameworks-are-supported-at-hostinger).

Choosing `Other`, changing the build command, or filling in an entry file does
not add a Python runtime. Use a VPS instead of the managed Node.js plan.

## Docker on a VPS

Install Docker, Caddy and Git on the VPS:

```bash
sudo apt update
sudo apt install -y docker.io caddy git
sudo systemctl enable --now docker
```

Clone the repository and build the image from the checked-in `Dockerfile`:

```bash
git clone https://github.com/WorshipWarehouse/Epub-cleaner.git
cd Epub-cleaner
sudo docker build -t epub-purifier .
```

Start the container. The container listens on `0.0.0.0:8000`; publishing only to
`127.0.0.1` keeps it off the public interface:

```bash
sudo docker run -d \
  --name epub-purifier \
  --restart unless-stopped \
  -p 127.0.0.1:8000:8000 \
  -v epub-purifier-data:/var/lib/epub-purifier \
  -e EPUB_PURIFIER_HOST=0.0.0.0 \
  -e EPUB_PURIFIER_PORT=8000 \
  epub-purifier
```

Check the app directly before involving Caddy:

```bash
curl -i http://127.0.0.1:8000/api/health
```

A healthy response is HTTP `200 OK` with JSON beginning
`{"status":"ok","version":"0.1.0",...}`. The root page is also served:

```bash
curl -I http://127.0.0.1:8000/
```

To update after a code change, rebuild and replace the container:

```bash
sudo docker build -t epub-purifier .
sudo docker stop epub-purifier
sudo docker rm epub-purifier
# then run the docker run command above again; the named volume keeps job
# metadata so files from before the restart still expire and are deleted
```

## Caddy reverse proxy

The checked-in `Caddyfile` proxies both hostnames to `127.0.0.1:8000` and caps
request bodies at `256MiB`, matching the app's `Limits.max_upload_bytes` of
`268435456` bytes. Caddy counts the whole multipart body, including framing, so
the proxy cap is defense in depth; the app still enforces its own 256 MiB file
cap after the request is accepted.

Before starting Caddy:

1. Point the `A`/`AAAA` records for `weareworshipwarehouse.online` and
   `www.weareworshipwarehouse.online` at the VPS.
2. Allow inbound TCP 80 and 443 (for example, `sudo ufw allow 80,443/tcp` if
   UFW is enabled).
3. Install the Caddyfile and validate it:

```bash
sudo cp Caddyfile /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
sudo systemctl enable --now caddy
sudo systemctl reload caddy
```

Caddy obtains and renews the TLS certificates automatically once DNS resolves
to the VPS. Verify the public endpoint:

```bash
curl -fsS https://weareworshipwarehouse.online/api/health
curl -fsS -o /dev/null -w '%{http_code}\n' https://weareworshipwarehouse.online/
```

## systemd alternative

Use this instead of Docker if the VPS already runs Python 3.10 or newer:

```bash
sudo apt update
sudo apt install -y python3 python3-venv git
python3 --version   # must be 3.10 or newer

sudo useradd --system --create-home \
  --home-dir /opt/epub-purifier \
  --shell /usr/sbin/nologin epub

sudo -u epub git clone \
  https://github.com/WorshipWarehouse/Epub-cleaner.git \
  /opt/epub-purifier/app

sudo -u epub python3 -m venv /opt/epub-purifier/app/.venv
sudo -u epub bash -c \
  'cd /opt/epub-purifier/app && .venv/bin/pip install ".[web]"'
```

Create `/etc/systemd/system/epub-purifier.service`:

```ini
[Unit]
Description=EPUB Purifier web app
After=network-online.target
Wants=network-online.target

[Service]
User=epub
Group=epub
WorkingDirectory=/opt/epub-purifier/app
Environment=EPUB_PURIFIER_HOST=127.0.0.1
Environment=EPUB_PURIFIER_PORT=8000
Environment=EPUB_PURIFIER_TRANSFER_DIR=/var/lib/epub-purifier/transfers
StateDirectory=epub-purifier
ExecStart=/opt/epub-purifier/app/.venv/bin/epub-purifier-web
Restart=on-failure
RestartSec=2
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true

[Install]
WantedBy=multi-user.target
```

Then start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now epub-purifier
sudo systemctl status epub-purifier
curl -i http://127.0.0.1:8000/api/health
```

The Caddyfile is unchanged: Caddy still proxies to `127.0.0.1:8000`.

## Runtime environment variables

These are read directly by the server. Restart the service or container after
changing one.

| Variable | Default | Meaning |
| --- | ---: | --- |
| `EPUB_PURIFIER_HOST` | `127.0.0.1` | Bind address. Use `0.0.0.0` only inside a container; keep loopback behind Caddy. |
| `EPUB_PURIFIER_PORT` | `8000` | HTTP port. Must match the Caddy upstream and Docker publish target. |
| `EPUB_PURIFIER_JOB_TTL` | `1800` | Seconds an uploaded inspection/clean job is retained. |
| `EPUB_PURIFIER_MAX_JOBS` | `32` | Maximum simultaneous uploaded jobs. |
| `EPUB_PURIFIER_RATE_LIMIT` | `20` | Upload/inspect requests per client per 60-second window. |
| `EPUB_PURIFIER_SEARCH_RATE_LIMIT` | `30` | Search requests per client per 60-second window. |
| `EPUB_PURIFIER_TRANSFER_RATE_LIMIT` | `10` | Source-transfer requests per client per 60-second window. |
| `EPUB_PURIFIER_TRANSFER_TTL` | `18000` | Seconds a fetched transfer is retained after the server starts downloading it (five hours). It is deleted at expiry whether or not it was downloaded. |
| `EPUB_PURIFIER_CLEANUP_INTERVAL` | `600` | Seconds between background cleanup passes (ten minutes). Expiry is also checked on every access. |
| `EPUB_PURIFIER_TRANSFER_DIR` | `<system temp>/epub-purifier-transfers` | Where cleaned downloads and their `job.json` metadata live. The default survives a process restart; point it at persistent storage (the Docker image uses `/var/lib/epub-purifier/transfers`) so cleanup also spans a reboot or a replaced container. |
| `EPUB_PURIFIER_MAX_TRANSFERS` | `16` | Maximum simultaneous source transfers. |
| `EPUB_PURIFIER_MAX_DOWNLOAD_MB` | `64` | Maximum fetched EPUB size, in MiB. |
| `EPUB_PURIFIER_MAX_STORAGE_MB` | `1024` | Maximum temporary storage used by cleaned downloads; new transfers are refused when it is full. |

There is **no** environment variable for the upload cap. It is the fixed
`Limits.max_upload_bytes` value: `256 * 1024 * 1024` bytes (256 MiB), enforced
in `src/epub_purifier/web/app.py`. If that value changes, update the `256MiB`
limit in `Caddyfile` at the same time.

## Security notes

- Caddy provides TLS and the 256 MiB body cap; it does **not** add user
  authentication. EPUB Purifier has no accounts. Keep it on a private network,
  or add an access-control layer such as Caddy `basic_auth`, Cloudflare Access,
  or a VPN before exposing it publicly.
- The container runs as the unprivileged `appuser` created in the Dockerfile.
- Temporary downloads live under a `0700` root with `0600` files, are scanned
  and cleaned before being served, and are removed five hours after the
  download starts. Mount the `/var/lib/epub-purifier` volume so restart-time
  cleanup sees the same job metadata; without it, a container's temporary
  directory starts empty.
- Do not remove the body cap, the app's own upload cap, or the rate limits to
  make deployment easier.
- Scanning is heuristic and structural, not a virus-signature engine. A report
  with no findings is not a guarantee that a file is safe.
