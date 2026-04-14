# Common Issues During Deployment

## Web container keeps restarting

### PermissionError on /app/staticfiles
```
PermissionError: [Errno 13] Permission denied: '/app/staticfiles'
```
The container user (UID 1000, appuser) can't write to directories mounted from the host. Fix:
```bash
chmod 777 staticfiles comms in_and_out data data/users
```
This affects ALL writable mount points, not just staticfiles.

### WhiteNoise MissingFileError on css/tailwindcss
```
whitenoise.storage.MissingFileError: The file 'css/tailwindcss' could not be found
```
WhiteNoise's `CompressedManifestStaticFilesStorage` parses CSS `@import` statements during `collectstatic` and chokes on Tailwind's `@import "tailwindcss"` in `input.css`. The fix is already in `fuzzyclaw/settings/prod.py` — it overrides STORAGES to use basic `StaticFilesStorage`. If you see this error, make sure `DJANGO_SETTINGS_MODULE=fuzzyclaw.settings.prod` is set in `.env`.

### Tailwind CSS not built
```
Built static/css/tailwind.css (0 bytes)
```
Or missing CSS on the login page. Run `./build_css.sh` on the host before starting containers. The Tailwind CLI must be installed: `tailwindcss --help`.

## Docker build OOM (exit code 137)

The first Docker build pulls base images and installs Python packages. On VMs with 8GB RAM or less, parallel builds can exhaust memory. The process gets killed with exit code 137.

Fix: retry the build. Cached layers from the first attempt make the second pass much lighter. If it keeps failing, add swap:
```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
```

## 400 Bad Request after starting

Django's `ALLOWED_HOSTS` is rejecting the request. Check that `.env` includes the hostname you're accessing — `localhost` for curl from the VM, the domain name for browser access, and optionally the IP.

Remember: `docker compose restart` does NOT re-read `.env`. Use `docker compose -f docker-compose.prod.yml --env-file .env up -d web` to apply env changes.

## Static files not loading (unstyled page)

Nginx needs execute permission on every directory in the path to `staticfiles/`. On Ubuntu, home directories are `drwxr-x---` by default — nginx (running as www-data) can't traverse them.

Fix:
```bash
chmod o+x /home/<user>
```

## Agent dispatch fails with Permission denied on /app/comms

Same as the staticfiles permission issue — the `comms/` directory needs to be writable:
```bash
mkdir -p comms && chmod 777 comms
```

## Fuzzy not responding on the board

Check fuzzy logs:
```bash
docker compose -f docker-compose.prod.yml --env-file .env logs fuzzy --tail=20
```

Common causes:
- `API_TOKEN` env var empty — fuzzy's platform queries return 401. Generate a token with `drf_create_token` and set `FUZZYCLAW_FUZZY_API_TOKEN` in `.env`, then recreate: `up -d fuzzy`.
- LLM API key missing — fuzzy needs at least one provider key to run its model.
- Redis not healthy — check `docker compose ps` for redis status.

## Certbot fails

- DNS not propagated yet — check at dnschecker.org
- Ports 80/443 not open in the cloud firewall — certbot needs HTTP for the ACME challenge
- Nginx not running or has a config error — `sudo nginx -t`
