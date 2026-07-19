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
- Zugriff auf die Image-Registry des Zenico-app-Images: `ghcr.io/gdsanger/zenico-app`
  ist ein **privates** GitHub-Container-Registry-Package. Der Docker-Host muss
  sich einmalig anmelden, bevor `docker compose pull` funktioniert:
  ```bash
  docker login ghcr.io -u gdsanger
  # Passwort: Personal Access Token mit Scope "read:packages" (nur lesend)
  ```
  Das Credential landet in `~/.docker/config.json` auf dem Host — **nicht**
  in `.env.agent` und nicht im Repo.
- Ein API-Token von Zenico.admin für die Agent-Authentifizierung
- **DNS:** Ein Wildcard-Record `*.zenico.app` (bzw. die genutzte Basis-Domain)
  muss auf die öffentliche IP des Hosts zeigen. Ohne diesen Record schlägt die
  Let's-Encrypt-HTTP-Challenge beim automatischen Anlegen des Proxy-Hosts fehl.

## DNS-Setup (einmalig, kein Pro-Instanz-Eintrag)

Statt für jede Kunden-Instanz einen eigenen DNS-Eintrag über eine
Registrar-API anzulegen, nutzt Zenico ein einmaliges Wildcard-Setup bei
[INWX](https://www.inwx.de/):

```
*.zenico.app.   A   <öffentliche IP von ig-srv-02, dem NPM-Host>
```

Jede Subdomain (`kunde-a.zenico.app`, `kunde-b.zenico.app`, …) löst damit
automatisch auf den NPM-Host auf — unabhängig davon, auf welchem Docker-Host
die jeweilige Instanz tatsächlich läuft. Neue Instanzen brauchen **keinen**
weiteren DNS-Eintrag; NPM übernimmt das Routing pro Subdomain (siehe
"Multi-Host-Betrieb" unten).

Let's Encrypt stellt die Zertifikate pro Subdomain über die HTTP-01-Challenge
aus (NPM-Default) — das funktioniert mit dem Wildcard-DNS-Eintrag problemlos,
weil jede Subdomain ja auf den NPM-Host auflöst, auf dem die Challenge
beantwortet wird. Eine DNS-01-Challenge (über die INWX-API) wäre nur nötig,
wenn ein echtes Wildcard-**Zertifikat** ausgestellt werden soll — das ist hier
nicht der Fall, jede Instanz bekommt ihr eigenes Einzel-Zertifikat.

## Multi-Host-Betrieb

Standardmäßig (`INSTANCE_FORWARD_HOST` leer) laufen NPM und alle
Kunden-Instanzen auf demselben Host (ig-srv-02) im gemeinsamen Docker-Netz
`npm_proxy`; NPM leitet dann per Container-Namen weiter (`{slug}-web-1:8000`).

Sobald eine Instanz auf einem anderen Host als NPM laufen soll, trägt das
gemeinsame Docker-Netz nicht mehr über die Hostgrenze hinweg. Für diesen Fall:

1. Auf dem Zielhost `INSTANCE_FORWARD_HOST` in `.env.agent` auf die von NPM
   aus erreichbare Adresse dieses Hosts setzen (IP oder DNS-Name).
2. Der Agent veröffentlicht dann den `web`-Service der Instanz auf einem
   Host-Port (`ports: - "<port>:8000"` statt des `npm_proxy`-Netzes) und legt
   den NPM-Proxy-Host mit `forward_host = INSTANCE_FORWARD_HOST` und
   `forward_port = <port>` an.
3. Der Health-Check läuft unverändert per `docker exec` direkt im Container —
   unabhängig vom gewählten Forwarding-Modus.

**Port-Vergabe:** Der Agent vergibt Host-Ports fortlaufend ab `WEB_PORT_BASE`
(Default `28000`), kollisionsfrei ermittelt durch Scan der vorhandenen
`docker-compose.yml`-Dateien unter `INSTANCES_DIR` — bewusst ohne eigene
Datenbank/State-Datei. Bei einem Retry nach `failed` wird ein bereits
vergebener Port für dieselbe Instanz wiederverwendet statt neu vergeben.
Läuft mehr als ein Host im Multi-Host-Modus, braucht jeder Host seinen
eigenen, nicht überlappenden `WEB_PORT_BASE` (z. B. Host A ab `28000`,
Host B ab `29000`) — die Port-Vergabe ist nur lokal pro Host kollisionsfrei,
da sie ausschließlich lokale `INSTANCES_DIR`-Einträge scannt.

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
| `INSTANCE_FORWARD_HOST` | Von NPM aus erreichbare Adresse dieses Hosts (optional, für Multi-Host-Betrieb — siehe unten) |
| `WEB_PORT_BASE` | Startwert für die Host-Port-Vergabe im Multi-Host-Modus (Default: 28000) |
| `NPM_API_URL` | Basis-URL des Nginx Proxy Manager (optional) |
| `NPM_API_EMAIL` | Login-E-Mail des NPM-API-Users (optional) |
| `NPM_API_PASSWORD` | Passwort des NPM-API-Users (optional) |

Sind `NPM_API_URL`, `NPM_API_EMAIL` und `NPM_API_PASSWORD` gesetzt, legt der
Agent nach dem Health-Check automatisch einen Proxy-Host mit Let's-Encrypt-
Zertifikat in NPM an (idempotent — ein Retry legt keinen zweiten Host an).
Fehlt eine der drei Variablen, bleibt dieser Schritt manuell und der Agent
gibt nur einen Log-Hinweis aus.

## Generierte Secrets pro Instanz

Beim Erst-Deployment generiert der Agent in der Kunden-`.env`:

| Variable | Zweck |
|---|---|
| `SECRET_KEY` | Django-Session-/Signing-Key |
| `FIELD_ENCRYPTION_KEY` | Fernet-Key für `encrypted_model_fields` (verschlüsselte DB-Felder, z. B. MailConfig/AzureSSOConfig-Secrets). Muss ein gültiger Fernet-Key sein (32 url-safe base64-kodierte Bytes **mit** Padding) — `secrets.token_urlsafe()` reicht dafür nicht aus. Ohne diesen Wert startet Django gar nicht (`ImproperlyConfigured`). |
| `DB_PASSWORD` | Passwort des Instanz-eigenen Postgres-Users |

**Re-Provisioning (Retry nach `failed`):** Existiert für die Instanz bereits
eine `.env`, generiert der Agent diese Secrets **nicht** neu, sondern lässt
die Datei unverändert. Ein neuer `FIELD_ENCRYPTION_KEY` würde bereits
verschlüsselte DB-Felder unlesbar machen, ein neues `DB_PASSWORD` passt nicht
mehr zum Passwort, mit dem Postgres im persistenten Volume initialisiert
wurde. Nur beim allerersten Lauf (kein `.env` vorhanden) werden neue Secrets
erzeugt.

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
