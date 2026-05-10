# Iris LiveKit Agent

Self-hosted LiveKit Agents worker that handles voice calls for the Lighthouse Inn.

## Stack

- **STT**: Deepgram Nova-3 (telephony-tuned, ~$0.005/min, sub-300 ms latency)
- **LLM**: Anthropic Claude (Haiku for hello-world, Sonnet for full Iris)
- **TTS**: Kokoro v1.0 (self-hosted on this droplet, voice `af_sarah`)
- **VAD + turn detection**: Silero VAD + LiveKit's turn-detector model

## First-time setup on a fresh droplet

Assumes the LiveKit Server stack is already running per `/opt/livekit/`.

```sh
# 1. System dep for Kokoro's phonemizer
sudo apt install -y espeak-ng

# 2. Python venv
cd /opt/iris-backend/agent
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e .

# 3. Download Kokoro model files (~360 MB total)
mkdir -p models
cd models
curl -L -O https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx
curl -L -O https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin
cd ..

# 4. Create .env from the template, fill in real values
cp .env.example .env
nano .env  # set LIVEKIT_*, ANTHROPIC_API_KEY, DEEPGRAM_API_KEY

# 5. Pre-download VAD + turn-detector models (one-time, ~50 MB)
.venv/bin/python hello_agent.py download-files

# 6. Verify in the foreground first
.venv/bin/python hello_agent.py dev
# (Call +1 541 991 5071 from your cell to test, then Ctrl+C)
```

## Install as a systemd service

Once the foreground run works:

```sh
sudo cp /opt/iris-backend/deploy/iris-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable iris-agent
sudo systemctl start iris-agent
sudo systemctl status iris-agent      # should show 'active (running)'
journalctl -u iris-agent -f           # tail logs
```

## Updating

After pulling new code:

```sh
cd /opt/iris-backend
git pull
cd agent
.venv/bin/pip install -e .            # only if pyproject.toml changed
sudo systemctl restart iris-agent
journalctl -u iris-agent -f
```
