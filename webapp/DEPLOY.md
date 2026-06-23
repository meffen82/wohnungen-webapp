# Wohnungssuche Web-App — Deploy

Eigener Docker-Container, isoliert von Hermes. Liest die Wohnungsdaten read-only,
hält Favoriten/Notizen/Hidden in einer eigenen `overlay-data/overlay.json`.

## Build & Run

```bash
cd /home/ubuntu/projects/wohnungssuche-app/webapp

docker build -t wohnungen-webapp:latest .

docker rm -f wohnungen-webapp 2>/dev/null

docker run -d --name wohnungen-webapp \
  --restart unless-stopped \
  --user 1001:1001 \
  -p 127.0.0.1:8765:8765 \
  -e WOHNUNGEN_SECRET=<SECRET> \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -v /.hermes/data:/data:ro \
  -v /home/ubuntu/projects/wohnungssuche-app/webapp/overlay-data:/overlay \
  --read-only --tmpfs /tmp \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --pids-limit 100 --memory 256m \
  wohnungen-webapp:latest
```

## Sicherheitsmodell

- **Read-only**: `/.hermes/data` ist `:ro` gemountet — die App kann die Hermes-Daten
  nur lesen, nie schreiben.
- **Kein Docker-Zugriff**: kein Docker-Socket im Container, kein `docker`-Binary.
  Die App kann den Host oder andere Container nicht erreichen.
- **Gehärtet**: `--read-only` rootfs, `--cap-drop ALL`, `no-new-privileges`,
  pids/memory-Limits, läuft als unprivilegierte uid 1001.
- **Eigene Daten**: nur `overlay-data/` ist beschreibbar (Favoriten/Notizen/Hidden).

## Zugriff

`https://host2.magrue.de/<SECRET>/` — der Secret-Token in der URL ist die Auth.
nginx (Host) proxyt nach `127.0.0.1:8765`, `access_log off` + `Referrer-Policy no-referrer`
im location-Block (Token-Leak-Schutz).

## Token rotieren

1. Neuen Token: `python3 -c "import secrets; print(secrets.token_urlsafe(24))"`
2. Container mit neuem `WOHNUNGEN_SECRET` neu starten (siehe Run oben).
3. nginx-`location`-Pfad in `/etc/nginx/sites-available/host2.magrue.de` auf den
   neuen Token ändern, `sudo nginx -t && sudo systemctl reload nginx`.
4. Neuen Link neu bookmarken/teilen.
