# zenico-provisioner

Eigenständiger Provisioning-Agent für [Zenico](https://zenico.app). Stellt
Kunden-Instanzen automatisch per Docker Compose bereit, sobald sie in
Zenico.admin angelegt wurden.

> Internes Tool für Perculasoft e.K. — kein öffentliches Projekt, kein
> Support für Dritte.

## Was macht das hier?

Der Agent läuft als Daemon auf dem Docker-Host und pollt Zenico.admin
periodisch nach neuen Instanzen. Für jede neue Instanz:

1. Claimt er sie (verhindert doppelte Bereitstellung bei Neustarts)
2. Generiert Secrets + rendert `docker-compose.yml` und `.env`
3. Startet die Instanz (`docker compose up -d`) — eigener Postgres-,
   Redis- und drei App-Container (web/worker/beat) aus demselben
   Zenico-app-Image
4. Wartet auf einen erfolgreichen Health-Check (`/healthz/`)
5. Legt (falls NPM konfiguriert) den Reverse-Proxy-Host inkl.
   Let's-Encrypt-Zertifikat in Nginx Proxy Manager an
6. Meldet Erfolg oder Fehler zurück an Zenico.admin

Kein SSH, kein Multi-Tenancy — jede Instanz ist ein eigener, isolierter
Satz Container (Docker-per-Customer, aus DSGVO-Gründen).

```
Zenico.admin  ──poll──>  zenico-provisioner  ──docker compose──>  Kunden-Instanz
   (Daten)                  (dieses Repo)                          (autark)
```

## Voraussetzungen

- Docker + Docker Compose Plugin auf dem Zielhost
- Externes Docker-Netzwerk für den Reverse Proxy:
  ```bash
  docker network create npm_proxy
  ```
  (Nginx Proxy Manager muss demselben Netzwerk angehören)
- Python 3.12
- Zugriff auf die Image-Registry des Zenico-app-Images
- Ein API-Token von Zenico.admin für die Agent-Authentifizierung
- **DNS:** Ein Wildcard-Record `*.zenico.app` (bzw. die genutzte Basis-Domain)
  muss auf die öffentliche IP des Hosts zeigen. Ohne diesen Record schlägt die
  Let's-Encrypt-HTTP-Challenge beim automatischen Anlegen des Proxy-Hosts fehl.

## Setup

```bash
git clone <repo-url> /srv/zenico/provisioning-agent
cd /srv/zenico/provisioning-agent

python -m venv venv
venv/bin/pip install -r requirements-agent.txt

cp .env.agent.example .env.agent
# .env.agent mit echtem ADMIN_API_TOKEN und ggf. anderen Werten befüllen

sudo cp zenico-provisioner.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now zenico-provisioner
```

Status prüfen:

```bash
sudo systemctl status zenico-provisioner
journalctl -u zenico-provisioner -f
```

## Konfiguration

Alle Einstellungen über `.env.agent` (siehe `.env.agent.example`):

| Variable | Bedeutung |
|---|---|
| `ADMIN_API_URL` | Basis-URL von Zenico.admin |
| `ADMIN_API_TOKEN` | Bearer-Token für die Agent-Authentifizierung |
| `DOCKER_IMAGE` | Image-Pfad des Zenico-app-Images |
| `INSTANCES_DIR` | Basisverzeichnis für generierte Instanz-Configs |
| `PROXY_NETWORK` | Name des externen Docker-Netzwerks für NPM |
| `POLL_INTERVAL` | Sekunden zwischen zwei Polls (Default: 30) |
| `HEALTH_TIMEOUT` | Sekunden bis ein Health-Check als gescheitert gilt |
| `NPM_API_URL` | Basis-URL des Nginx Proxy Manager (optional) |
| `NPM_API_EMAIL` | Login-E-Mail des NPM-API-Users (optional) |
| `NPM_API_PASSWORD` | Passwort des NPM-API-Users (optional) |

Sind `NPM_API_URL`, `NPM_API_EMAIL` und `NPM_API_PASSWORD` gesetzt, legt der
Agent nach dem Health-Check automatisch einen Proxy-Host mit Let's-Encrypt-
Zertifikat in NPM an (idempotent — ein Retry legt keinen zweiten Host an).
Fehlt eine der drei Variablen, bleibt dieser Schritt manuell und der Agent
gibt nur einen Log-Hinweis aus.

## Projektstruktur

```
zenico-provisioner/
├── agent.py                       # Haupt-Loop
├── templates/
│   ├── docker-compose.yml.j2
│   └── env.j2
├── requirements-agent.txt
├── zenico-provisioner.service
├── .env.agent.example
├── CLAUDE.md                      # Leitplanken für Claude Code
└── README.md
```

## Was (noch) nicht gemacht wird

- Backups pro Kunden-DB (separater Cronjob)
- Updates bestehender Instanzen ausrollen (bewusst manuell)

## Verwandte Projekte

- **Friday / Zenico.app** — liefert das Docker-Image
- **Zenico.admin** — liefert die Instanz-Daten über die API

Details und Entwicklungs-Konventionen: siehe [`CLAUDE.md`](./CLAUDE.md).
