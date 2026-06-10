# VPS Deployment

This deployment runs the Discord listener, dry-run executor, API, and dashboard
with Docker Compose. It intentionally cannot place real broker orders.

## Safety Model

- `EXECUTOR_MODE=DRY_RUN` is forced in `compose.yaml`.
- Runtime state is persisted in `./logs` on the VPS.
- The dashboard binds to VPS localhost only. View it through an SSH tunnel.
- `.env` remains on the VPS and must never be committed.

## First-Time VPS Setup

Install Docker and verify it:

```bash
apt update
apt install -y docker.io docker-compose-v2 git
systemctl enable --now docker
docker run --rm hello-world
```

Create a non-root deployment user:

```bash
adduser deploy
usermod -aG docker deploy
install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
cp /root/.ssh/authorized_keys /home/deploy/.ssh/authorized_keys
chown deploy:deploy /home/deploy/.ssh/authorized_keys
chmod 600 /home/deploy/.ssh/authorized_keys
```

Open a new local terminal and verify access before disabling root SSH:

```powershell
ssh deploy@YOUR_DROPLET_IP
```

## Deploy

The repository must be available from a private Git remote that the VPS can
read. As the `deploy` user:

```bash
git clone YOUR_PRIVATE_REPOSITORY_URL trade-bot
cd trade-bot
cp .env.example .env
nano .env
mkdir -p logs
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 listener executor
```

At minimum, populate the Discord token and channel IDs in `.env`. Keep:

```dotenv
EXECUTOR_MODE=DRY_RUN
```

## View the Private Dashboard

From local PowerShell, create an SSH tunnel:

```powershell
ssh -L 8080:127.0.0.1:8080 deploy@YOUR_DROPLET_IP
```

Keep that session open and visit:

```text
http://127.0.0.1:8080
```

## Operations

```bash
docker compose ps
docker compose logs -f listener executor
docker compose restart listener
git pull
docker compose up -d --build
```

Back up `./logs` off the VPS before relying on it as the only audit history.
