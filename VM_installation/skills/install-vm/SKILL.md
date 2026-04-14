---
name: install-vm
description: Deploy FuzzyClaw to a production VM with Docker, nginx, and SSL. Use whenever the user wants to install FuzzyClaw on a remote server, deploy to a VM, set up a production instance, or mentions "deploy FuzzyClaw", "install on server", "production setup", or "VM deployment". Also trigger when the user has a fresh VM and wants to get FuzzyClaw running.
---

# Deploy FuzzyClaw to a VM

This skill walks you through deploying FuzzyClaw to a Linux VM with Docker, nginx, HTTPS, and all 6 services running. The result is a production instance accessible only via HTTPS on a custom domain.

You are guiding the user step-by-step. Some steps require sudo or interactive input (passwords, API keys) — pause and hand control to the user for those. Everything else, you drive.

## Prerequisites

Before starting, confirm with the user:

1. **A Linux VM** with SSH access — any distro (Ubuntu, Debian, RHEL, etc.). The user must be able to run sudo.
2. **SSH reachable by the agent** — you need to be able to `ssh <host>` from the local machine and get a shell. Test this first. If it fails, the user needs to fix their SSH config before you can proceed.
3. **A domain name** (or subdomain) that the user can point to the VM's IP address. They should create a DNS A record pointing to the VM's IP. This is needed for HTTPS (certbot) and should be done early — DNS propagation can take minutes. Check at dnschecker.org.
4. **At least one LLM API key** (OpenAI, Google, or Anthropic).

## Workflow

Work through these phases in order. If a step fails, diagnose before moving on — don't skip ahead.

### Phase 1: System Setup

```bash
# 1. Verify you can SSH in
ssh <host> "whoami && uname -a && df -h / && free -h"

# 2. Install Docker
# USER RUNS THIS (requires sudo):
# curl -fsSL https://get.docker.com | sudo sh
# sudo usermod -aG docker $USER
# Then log out/in for the group to take effect

# 3. Verify Docker (after re-login)
ssh <host> "docker compose version"

# 4. Install nginx + certbot
# USER RUNS (sudo). Adapt the package manager to the distro:
# Debian/Ubuntu: sudo apt update && sudo apt install -y nginx certbot python3-certbot-nginx
# RHEL/Fedora:   sudo dnf install -y nginx certbot python3-certbot-nginx
# Other:         install nginx and certbot via the distro's package manager

# 5. Install Tailwind CSS CLI
ssh <host> "curl -sLO https://github.com/tailwindlabs/tailwindcss/releases/latest/download/tailwindcss-linux-x64 && chmod +x tailwindcss-linux-x64 && sudo mv tailwindcss-linux-x64 /usr/local/bin/tailwindcss"
```

### Phase 2: Clone and Configure

```bash
# 1. Clone the repo
ssh <host> "cd ~ && git clone https://github.com/andremoreira73/fuzzyclaw.git"

# 2. Copy env template
ssh <host> "cd ~/fuzzyclaw && cp .env.example .env"

# 3. Generate secrets
ssh <host> "python3 -c \"import secrets; print('DB_PASSWORD:', secrets.token_urlsafe(24))\""
ssh <host> "python3 -c \"import secrets; print('DJANGO_SECRET_KEY:', secrets.token_urlsafe(50))\""

# 4. Get Docker socket GID
ssh <host> "stat -c '%g' /var/run/docker.sock"
```

Now pause and tell the user to edit `~/fuzzyclaw/.env` with:

| Variable | Value |
|----------|-------|
| `DB_PASSWORD` | generated above |
| `DJANGO_SECRET_KEY` | generated above |
| `DOCKER_GID` | from stat command above |
| `DJANGO_SETTINGS_MODULE` | `fuzzyclaw.settings.prod` |
| `POSTGRES_DB` | `fuzzyclaw_prod` |
| `POSTGRES_USER` | `fuzzyclawuser` |
| `ALLOWED_HOSTS` | `<domain>,<VM_IP>,localhost` |
| `CSRF_TRUSTED_ORIGINS` | `https://<domain>` |
| LLM API keys | at least one of OPENAI/GOOGLE/ANTHROPIC |

**Do NOT proceed until the user confirms the `.env` is configured.** Never write API keys yourself.

### Phase 3: Build and Deploy

```bash
# 1. Build Tailwind CSS
ssh <host> "cd ~/fuzzyclaw && ./build_css.sh"

# 2. Fix writable directory permissions (container user is UID 1000)
ssh <host> "cd ~/fuzzyclaw && mkdir -p staticfiles comms in_and_out data/users && chmod 777 staticfiles comms in_and_out data data/users"

# 3. Make the home directory traversable by nginx
ssh <host> "chmod o+x \$HOME"

# 4. Build Docker images
ssh <host> "cd ~/fuzzyclaw && docker compose -f docker-compose.prod.yml --env-file .env build"

# 5. Start all services
ssh <host> "cd ~/fuzzyclaw && docker compose -f docker-compose.prod.yml --env-file .env up -d"

# 6. Verify all 6 services are running
ssh <host> "cd ~/fuzzyclaw && docker compose -f docker-compose.prod.yml --env-file .env ps"
```

Expected: db, redis, web, celery, celery-beat, fuzzy — all running.

If web is restarting, check logs: `docker compose -f docker-compose.prod.yml --env-file .env logs web --tail=20`

Common issues at this stage — see [references/common-issues.md](references/common-issues.md).

### Phase 4: Post-Deploy Setup

```bash
# 1. Verify dashboard responds
ssh <host> "curl -s -o /dev/null -w '%{http_code}' http://localhost:8200/accounts/login/"
# Expected: 200

# 2. Create superuser — USER RUNS THIS (interactive):
# ssh -t <host> "cd ~/fuzzyclaw && docker compose -f docker-compose.prod.yml --env-file .env exec web python manage.py createsuperuser"

# 3. Build agent images (can take a few minutes on first run)
ssh <host> "cd ~/fuzzyclaw && docker compose -f docker-compose.prod.yml --env-file .env exec web python manage.py sync_images"

# 4. Verify agents and skills
ssh <host> "cd ~/fuzzyclaw && docker compose -f docker-compose.prod.yml --env-file .env exec web python manage.py check_agents && docker compose -f docker-compose.prod.yml --env-file .env exec web python manage.py check_skills"

# 5. Create fuzzy API token — USER RUNS:
# ssh -t <host> "cd ~/fuzzyclaw && docker compose -f docker-compose.prod.yml --env-file .env exec web python manage.py drf_create_token <superuser_username>"
# Then add FUZZYCLAW_FUZZY_API_TOKEN=<token> to .env

# 6. Recreate fuzzy to pick up the token (up -d, not restart — restart doesn't re-read .env)
ssh <host> "cd ~/fuzzyclaw && docker compose -f docker-compose.prod.yml --env-file .env up -d fuzzy"
```

### Phase 5: Nginx + SSL

Before this step, the user's domain must resolve to the VM's IP. Verify:

```bash
nslookup <domain> 8.8.8.8
```

If it doesn't resolve yet, wait. DNS propagation can take a few minutes — check at dnschecker.org. This is why the skill asks the user to set up DNS early in the prerequisites.

Once DNS is ready:

1. **Create the nginx site config** from the template at [assets/nginx/site.conf.template](assets/nginx/site.conf.template). Replace `{{DOMAIN}}` and `{{PROJECT_PATH}}` with the actual values. Write it to `/etc/nginx/sites-available/<domain>` on the VM (requires sudo).

2. **Enable and test:**
```bash
# USER RUNS (sudo):
# sudo ln -sf /etc/nginx/sites-available/<domain> /etc/nginx/sites-enabled/
# sudo rm -f /etc/nginx/sites-enabled/default
# sudo nginx -t && sudo systemctl reload nginx
```

3. **Get SSL certificate — USER RUNS:**
```bash
# sudo certbot --nginx -d <domain>
```

4. **Block direct IP access.** Create a catch-all server block from [assets/nginx/catch-all.conf.template](assets/nginx/catch-all.conf.template). Replace `{{DOMAIN}}` and write to `/etc/nginx/sites-available/catch-all` (requires sudo).

```bash
# USER RUNS (sudo):
# sudo ln -sf /etc/nginx/sites-available/catch-all /etc/nginx/sites-enabled/
# sudo nginx -t && sudo systemctl reload nginx
```

5. **Open firewall ports.** Only ports 80 and 443 should be open. Do NOT open port 8200 — nginx proxies to it on localhost. The method depends on the environment:
   - **Cloud providers** (GCP, AWS, Azure): configure firewall rules in the cloud console
   - **On-premise / bare metal:** `sudo ufw allow 80/tcp && sudo ufw allow 443/tcp` (or iptables equivalent)

### Phase 6: Verify Production

```bash
# Test HTTPS
curl -s -o /dev/null -w '%{http_code}' https://<domain>/accounts/login/
# Expected: 200

# Test direct IP is blocked
curl -s -o /dev/null -w '%{http_code}' --max-time 5 https://<VM_IP>/ 2>/dev/null
# Expected: 000 (connection dropped)
```

Then ask the user to:
1. Open `https://<domain>` in their browser
2. Log in with the superuser account
3. Check the file manager
4. Send a message to fuzzy on the board
5. Launch a test briefing

If client-specific skills need to be copied (not in git):
```bash
scp -r skills/<skill-name> <host>:~/fuzzyclaw/skills/
```

### Phase 7: Ongoing Deployment

After the initial setup, the deploy cycle is:

```bash
# On VM: pull latest code and redeploy
cd ~/fuzzyclaw
git pull origin main
./docker_prod.sh deploy
```

The `docker_prod.sh` script handles: build, up, migrate, sync_images, check_agents, check_skills.

Other useful commands:
- `./docker_prod.sh logs web` — follow web logs
- `./docker_prod.sh fuzzy-logs` — follow fuzzy logs
- `./docker_prod.sh sync-agents` — rebuild agent images + restart celery
- `./docker_prod.sh status` — container status + resource usage

## Important Notes

- `docker compose restart` does NOT re-read `.env`. Use `docker compose -f docker-compose.prod.yml --env-file .env up -d <service>` to recreate a container with updated env vars.
- The `staticfiles/`, `comms/`, `in_and_out/`, and `data/` directories must be writable by UID 1000 (the container's appuser). The volume mount preserves host ownership, so `chmod 777` is needed on the host.
- Uvicorn binds to `127.0.0.1:8200` in production — only reachable from localhost (nginx). This is set in `docker-compose.prod.yml`.
- SSL certificates auto-renew via certbot's systemd timer. Verify: `sudo systemctl status certbot.timer`.
