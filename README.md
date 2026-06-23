# Wohnungssuche MA/LU — Dashboard

Interaktives Web-Dashboard für die tägliche Wohnungssuche in Mannheim und Ludwigshafen.

Der Hermes-Agent sucht täglich nach Inseraten und schreibt die Ergebnisse nach `apartment_state.json`. Diese Web-App zeigt die Daten als interaktives Dashboard an.

## Features

- **Karten-Grid** mit allen Wohnungsinfos: ID, Rang, Titel, Ort, Kosten, Ausstattung
- **Rang-Badge** (🔝🌳🍳✅📋) basierend auf Badewanne / Terrasse / Balkon / EBK
- **Favoriten** mit Datum und Notiz — bleiben auch nach Hermes-Prune erhalten (Snapshot)
- **Ausblenden** von uninteressanten Inseraten
- **Sektionen:** Favoriten · Neu (letzte 7 Tage) · Alle · Ausgeblendet · Verschwundene Favoriten
- **Filter:** Stadt (MA/LU), Ausstattungs-Chips (🛁🪴🌳🍳), Sortierung (Datum/Rang/Preis)
- **Auto-Refresh** mit Update-Banner wenn neue Daten vorliegen
- **Token-Sicherheit:** URL-Token als einzige Auth, kein Login erforderlich
- **PWA-fähig:** installierbar auf iPhone/Android
- **Gehärteter Container:** read-only rootfs, cap-drop ALL, unprivilegierter User

## Architektur

```
Hermes-Agent  →  apartment_state.json  (read-only mount)
                        ↓
               wohnungen-webapp Container
                        ↓
                   nginx (Host)
                        ↓
           https://<APP_URL>/<TOKEN>/
```

Die App schreibt **nie** in die Hermes-Daten. Favoriten, Notizen und Hidden-Flags werden in einer eigenen `overlay.json` gespeichert.

## Deployment

Siehe [webapp/DEPLOY.md](webapp/DEPLOY.md) für den vollständigen Build- und Run-Befehl.

```bash
cd webapp
docker build -t wohnungen-webapp:latest .
docker rm -f wohnungen-webapp
docker run -d --name wohnungen-webapp \
  --restart unless-stopped \
  --user 1001:1001 \
  -p 127.0.0.1:8765:8765 \
  -e WOHNUNGEN_SECRET=<TOKEN> \
  -v /.hermes/data:/data:ro \
  -v overlay-data:/overlay \
  --read-only --tmpfs /tmp \
  --cap-drop ALL --security-opt no-new-privileges \
  wohnungen-webapp:latest
```

## Tech-Stack

- **Python 3.12** + **FastAPI** + **uvicorn**
- Server-seitiges HTML-Rendering (kein JavaScript-Framework)
- Docker (python:3.12-slim)
