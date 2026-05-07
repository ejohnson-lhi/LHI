# Iris backend — DigitalOcean deployment

End-to-end recipe for deploying the Iris backend on a DigitalOcean droplet.
Substitute `iris.lighthouseinn-florence.com` for whatever subdomain you
actually use, and `<droplet-ipv4>` for the droplet's IP.

## 1. Provision the droplet

In the DigitalOcean console (`cloud.digitalocean.com`):

1. **Create Droplet** → region **SFO3** → image **Ubuntu 24.04 LTS x64** → plan **Basic** ($6/mo, 1 GB RAM, 1 vCPU, 25 GB SSD).
2. **Authentication**: choose SSH key. Add yours if it isn't there yet.
3. **Backups** (recommended): enable. Adds 20% (+$1.20/mo) for weekly snapshots.
4. **Hostname**: `iris-backend` or similar.
5. Note the droplet's IPv4 address once it's provisioned. 64.23.167.164, 10.124.0.2

## 2. DNS

In whatever DNS host manages your domain:

- Add an **A record**: `iris` → `<droplet-ipv4>` (TTL 3600).
- Verify propagation: `dig +short iris.lighthouseinn-florence.com` should return the droplet IP.

## 3. Initial droplet setup

SSH in as root:

```sh
ssh root@<droplet-ipv4>
```

Then run:

```sh
# 1. Update system packages
apt update && apt upgrade -y

# 2. Create unprivileged service user
adduser --disabled-password --gecos "" iris
usermod -aG sudo iris

# 3. Mirror your SSH key for the iris user
mkdir -p /home/iris/.ssh
cp /root/.ssh/authorized_keys /home/iris/.ssh/
chown -R iris:iris /home/iris/.ssh
chmod 700 /home/iris/.ssh
chmod 600 /home/iris/.ssh/authorized_keys

# 4. Disable root SSH and password auth
sed -i 's/^#*PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl reload ssh

# 5. Basic firewall
apt install -y ufw
ufw allow OpenSSH
ufw allow 80
ufw allow 443
ufw --force enable
```

Log out and reconnect as `iris`:

```sh
ssh iris@<droplet-ipv4>
```

## 4. Install Python + git

```sh
sudo apt install -y python3 python3-venv python3-pip git
```

## 5. Clone the repo

```sh
sudo mkdir -p /opt/iris-backend
sudo chown iris:iris /opt/iris-backend
cd /opt/iris-backend
git clone https://github.com/<your-github-username>/<your-repo-name>.git .
```

For a private repo you'll need either an HTTPS personal access token or an SSH key registered with GitHub. SSH key is cleaner — generate one on the droplet (`ssh-keygen -t ed25519`) and add the public key under your GitHub Settings → SSH keys, then `git clone git@github.com:...`.

## 6. Install Python dependencies

```sh
cd /opt/iris-backend/backend
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e .
```

## 7. Configure .env

```sh
cp .env.example .env
nano .env
```

Set `APP_ENV=production` and fill in real credentials for:

- `CLOUDBEDS_API_KEY`, `CLOUDBEDS_PROPERTY_ID`, `CLOUDBEDS_IRIS_USER_ID`, `CLOUDBEDS_RESERVATION_SOURCE_ID`
- `ANTHROPIC_API_KEY`
- `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_HOTEL_NUMBER`, `TWILIO_MESSAGING_SERVICE_SID` (once A2P approved)
- `VAPI_API_KEY`, `VAPI_ASSISTANT_ID` (production assistant — see step 11)
- `ADMIN_SIP_HEADER_SECRET`, `ERIC_CELL_NUMBER`

Permissions on the file:

```sh
chmod 600 .env
```

## 8. Create the SQLite data directory

```sh
mkdir -p /opt/iris-backend/backend/data
```

## 9. Install the systemd unit

```sh
sudo cp /opt/iris-backend/deploy/iris-backend.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable iris-backend
sudo systemctl start iris-backend
sudo systemctl status iris-backend
```

Should show `active (running)`. Live logs:

```sh
journalctl -u iris-backend -f
```

Verify the service is listening locally:

```sh
curl http://127.0.0.1:8000/health
```

## 10. Install + configure Caddy

```sh
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update
sudo apt install -y caddy

# Caddy reads /etc/caddy/Caddyfile by default
sudo cp /opt/iris-backend/deploy/Caddyfile /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Caddy auto-fetches a Let's Encrypt cert on the first HTTPS request. Test:

```sh
curl -i https://iris.lighthouseinn-florence.com/health
```

Should return `{"status":"ok",...}` with a `200 OK` and a valid TLS cert.

## 11. Wire Vapi to the production URL

From your local Windows machine, with the prod assistant's `VAPI_ASSISTANT_ID` set in `.env`:

```cmd
backend\.venv\Scripts\python.exe backend\scripts\sync_to_vapi.py https://iris.lighthouseinn-florence.com
```

If you maintain a separate dev assistant for local-tunnel testing, sync that one with the cloudflared URL when you're iterating; sync the prod assistant with the droplet URL only after you're happy with the changes.

## Updating

To deploy a code change:

```sh
ssh iris@<droplet-ipv4>
cd /opt/iris-backend
git pull
cd backend
.venv/bin/pip install -e .   # only if pyproject.toml dependencies changed
sudo systemctl restart iris-backend
```

If the system prompt or KB changed, re-run `sync_to_vapi.py` from your local machine afterwards (or from the droplet — it just needs `VAPI_API_KEY` + `VAPI_ASSISTANT_ID` in `.env`).

## Troubleshooting

- **Service won't start**: `journalctl -u iris-backend -n 100` shows the last 100 log lines.
- **502 from Caddy**: the FastAPI process isn't listening; check `systemctl status iris-backend`.
- **TLS cert never issued**: usually means port 80 isn't open or DNS hasn't propagated. Check `dig +short iris.lighthouseinn-florence.com` and `sudo ufw status`.
- **`.env` change not taking effect**: `--reload` isn't used in production, but `systemctl restart iris-backend` re-reads `.env` from disk.
