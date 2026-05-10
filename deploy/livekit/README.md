# LiveKit stack for Iris

Self-hosted LiveKit Server + SIP gateway + Egress, all in Docker. This is the
voice infrastructure for the Iris AI receptionist on the droplet.

Files in this directory:

```
docker-compose.yml          # 4 services: redis, livekit-server, livekit-sip, egress
livekit.yaml.example        # LiveKit Server config template
sip.yaml.example            # LiveKit SIP gateway config template
egress.yaml.example         # LiveKit Egress config template
config/
  inbound-trunk.json.example   # SIP inbound trunk (IP-based ACL for Twilio)
  dispatch-rule.json.example   # Per-call dispatch rule + auto-egress audio recording
```

The `*.example` files are checked in. The real `*.yaml` and `config/*.json`
files (which contain secrets and the live trunk SID) are gitignored — copy
from the templates and fill in actual values once on the droplet.

## First-time setup on a fresh droplet

```sh
cd /opt/iris-backend/deploy/livekit

# 1. Make recordings dir (audio + transcripts both live here; transcripts
#    are written by the agent process, audio by egress)
sudo mkdir -p /opt/iris-backend/recordings
sudo chmod 777 /opt/iris-backend/recordings

# 2. Generate LiveKit API credentials
LK_KEY="API$(openssl rand -hex 8)"
LK_SECRET="$(openssl rand -hex 32)"
echo "Save these in your password manager:"
echo "  LK_KEY=$LK_KEY"
echo "  LK_SECRET=$LK_SECRET"

# 3. Create the three YAML configs from templates and fill in keys
cp livekit.yaml.example livekit.yaml
cp sip.yaml.example sip.yaml
cp egress.yaml.example egress.yaml

# Replace placeholders in all three (use sed or VS Code Remote SSH)
sed -i "s|APIxxxxxxxxxxxxxx|$LK_KEY|g" livekit.yaml sip.yaml egress.yaml
sed -i "s|xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx|$LK_SECRET|g" livekit.yaml sip.yaml egress.yaml

# 4. Bring it up
docker compose up -d
docker compose ps  # all 4 should show 'Up'

# 5. Configure the SIP inbound trunk + dispatch rule via lk CLI
cp config/inbound-trunk.json.example config/inbound-trunk.json
lk sip inbound create config/inbound-trunk.json
lk sip inbound list  # note the SipTrunkID

# Edit config/dispatch-rule.json to substitute the real trunk SID
cp config/dispatch-rule.json.example config/dispatch-rule.json
nano config/dispatch-rule.json  # or VS Code Remote SSH; replace ST_REPLACE_WITH_REAL_TRUNK_ID
lk sip dispatch create config/dispatch-rule.json
lk sip dispatch list

# 6. Verify by placing a test call. After hangup:
ls -la /opt/iris-backend/recordings/  # should show .ogg + .json files
```

## Day-to-day operation

```sh
cd /opt/iris-backend/deploy/livekit

docker compose ps           # status
docker compose logs -f egress           # tail egress logs
docker compose logs -f livekit-sip      # tail SIP signaling
docker compose restart livekit-sip      # restart one service
docker compose down && docker compose up -d  # full restart
```

## Migrating from the older `/opt/livekit/` location

If you previously had everything at `/opt/livekit/`, here's the move:

```sh
cd /opt/iris-backend/deploy/livekit
git pull  # gets this directory + the templates

# Stop old stack
cd /opt/livekit && docker compose down

# Copy your existing secrets-bearing configs into the new location
sudo cp /opt/livekit/livekit.yaml /opt/iris-backend/deploy/livekit/
sudo cp /opt/livekit/sip.yaml /opt/iris-backend/deploy/livekit/
sudo chown iris:iris /opt/iris-backend/deploy/livekit/livekit.yaml /opt/iris-backend/deploy/livekit/sip.yaml

# Create new egress.yaml from template (use same key/secret as livekit.yaml)
cd /opt/iris-backend/deploy/livekit
cp egress.yaml.example egress.yaml
nano egress.yaml  # fill in the real key/secret

# Move the SIP config files (trunk + dispatch rule) too
sudo mv /opt/livekit/config/*.json /opt/iris-backend/deploy/livekit/config/

# Bring up the new stack
docker compose up -d
docker compose ps

# Verify, then remove the old location
sudo rm -rf /opt/livekit/
```

## Recordings

- **Audio (OGG)**: written by egress to `/opt/iris-backend/recordings/<room-name>-<timestamp>.ogg`. ~0.24 MB/min.
- **Transcripts (JSON)**: written by the agent process (see `agent/iris_agent.py`) to the same directory. Per-call file with full chat history + timestamps.

To prevent disk-fill, add a daily cleanup cron on the droplet:

```sh
sudo tee /etc/cron.daily/iris-recordings-cleanup <<'EOF'
#!/bin/sh
find /opt/iris-backend/recordings -name "*.ogg" -mtime +14 -delete
find /opt/iris-backend/recordings -name "*.json" -mtime +30 -delete
EOF
sudo chmod +x /etc/cron.daily/iris-recordings-cleanup
```

(Audio retained 14 days, transcripts 30 days. Adjust as needed.)
