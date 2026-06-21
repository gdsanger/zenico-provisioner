# CLAUDE.md — zenico-provisioner

Kontext und Leitplanken für Claude Code in diesem Repository.

## Was ist dieses Projekt?

Eigenständiger Provisioning-Agent für Zenico. Läuft als Daemon auf dem
Docker-Host und stellt Kunden-Instanzen automatisch bereit, sobald sie in
Zenico.admin als "pending" markiert wurden.

**Wichtig: Dies ist NICHT Teil von Zenico.admin und NICHT Teil von
Friday/Zenico.app.** Es ist ein eigenständiges drittes Projekt, das beide
über HTTP-API (Admin) bzw. Docker Image (App) konsumiert, aber nicht deren
Code enthält oder verändert. Separate Repos sind hier bewusst gewählt —
siehe Begründung unten unter "Verwandte Projekte".

## Architektur / Ablauf

```
Zenico.admin (Daten)              zenico-provisioner (dieses Repo)
─────────────────────             ──────────────────────────────────
status=pending  ◄──── poll ────   GET /api/instances/pending/
                                   POST .../claim/        (→ provisioning)
                                          │
                                          ├─ Verzeichnis anlegen
                                          ├─ docker-compose.yml rendern
                                          ├─ .env rendern (Secrets generieren)
                                          ├─ docker compose pull && up -d
                                          └─ /healthz/ pollen
                                          │
status=running   ◄──── report ────  POST .../complete/
status=failed    ◄──── report ────  POST .../fail/
```

Jede Kunden-Instanz ist vollständig autark: eigener Postgres-Container,
eigener Redis-Container, drei Rollen (`web`/`worker`/`beat`) aus demselben
Zenico-app-Image. Kein SSH — der Agent läuft lokal auf demselben Host wie
die Kunden-Container.

## Tech Stack

- Python 3.12, keine Web-Framework-Abhängigkeit (kein Django hier nötig)
- `requests` für die Admin-API-Kommunikation
- `jinja2` für Compose-/Env-Templates
- `subprocess` für `docker compose`-Aufrufe (kein Docker SDK, bewusst simpel)
- systemd für den Dauerbetrieb (kein Kubernetes, kein Supervisor-Overhead)

## Repo-Struktur

```
zenico-provisioner/
├── agent.py                       # Haupt-Loop (Poll → Claim → Provision → Report)
├── templates/
│   ├── docker-compose.yml.j2      # Pro-Instanz-Compose-Template
│   └── env.j2                     # Pro-Instanz-.env-Template
├── requirements-agent.txt
├── zenico-provisioner.service     # systemd Unit
├── .env.agent.example             # Vorlage — NIEMALS echte Werte committen
└── README.md
```

## Harte Regeln (nicht verhandelbar)

- **Keine Secrets im Repo.** `.env.agent` (echte Werte) ist `.gitignore`t,
  nur `.env.agent.example` wird committet. Gleiches gilt für generierte
  Kunden-`.env`-Dateien — die landen ausschließlich unter `INSTANCES_DIR`
  auf dem Zielhost, niemals im Repo.
- **Kein SSH-Code.** Der Agent läuft lokal auf dem Docker-Host. Falls sich
  das Hosting-Modell mal ändert (mehrere Hosts), ist das eine bewusste
  Architekturentscheidung, kein Nebenbei-Feature — vorher ansprechen.
- **Kein Multi-Tenancy-Versuch.** Eine Instanz = ein eigener Satz Container
  (Docker-per-Customer), analog zur Friday/Zenico.app-Architektur. Nicht
  "optimieren" zu shared Containern — das war eine bewusste DSGVO-Entscheidung.
- **DSGVO:** Hosting bleibt auf Hetzner Cloud / Deutschland. Keine Calls an
  US-Dienste für irgendetwas, das Kundendaten enthalten könnte.
- **Idempotenz beim Claim.** Das `claim()`-Pattern verhindert Doppel-
  Provisionierung nach einem Agent-Neustart. Nicht durch einfaches
  Status-Update ohne Atomarität ersetzen.
- **Kein eigenes Dockerfile in diesem Repo.** Das Image kommt fertig gebaut
  aus dem Friday/Zenico.app-Repo und wird hier ausschließlich über die
  `DOCKER_IMAGE`-Env-Var referenziert.

## Git Workflow

- Niemals direkt auf `main` committen.
- Vor jeder Änderung einen neuen Branch von `main` erstellen
  (Namensschema: `feature/<kurzbeschreibung>` oder `fix/<kurzbeschreibung>`).
- Nach Abschluss: `gh pr create --draft` mit aussagekräftiger Beschreibung
  der Änderungen.
- `main` bleibt geschützt, nur via Review-PR mergen.

## Coding-Konventionen

- Log-Messages und Kommentare auf Deutsch (konsistent mit den anderen
  Zenico-Repos), Funktions-/Variablennamen auf Englisch.
- Keine neuen Abhängigkeiten ohne triftigen Grund — das Script soll bewusst
  schlank bleiben (Solo-Maintainer-Projekt, kein Enterprise-Tooling-Overhead).
- Fehler werden immer an Zenico.admin zurückgemeldet (`fail()`), nie nur
  geloggt und verschluckt — sonst bleibt eine Instanz für immer "pending"
  oder hängt unsichtbar in "provisioning".

## Verwandte Projekte (nicht in diesem Repo)

- **Friday / Zenico.app** — liefert das Docker-Image, das hier konsumiert
  wird (`DOCKER_IMAGE` Env-Var). Health-Check-Endpoint `/healthz/` ist dort
  bereits implementiert.
- **Zenico.admin** — liefert die Instanz-Daten über die API
  (`/api/instances/pending/`, `/claim/`, `/complete/`, `/fail/`). Diese
  Endpoints müssen ggf. noch dort ergänzt werden — das ist ein Issue im
  Zenico.admin-Repo, nicht hier.

## Was noch offen / bewusst nicht gebaut ist

- NPM-Proxy-Host + Let's-Encrypt-Zertifikat automatisch anlegen (aktuell
  manueller Schritt) — eigenes Issue, sobald NPM-API-Anbindung gebaut wird.
- Backups pro Kunden-DB — separater Cronjob, nicht Teil dieses Agenten.
- Updates bestehender Instanzen ausrollen — bewusst (noch) manuell, siehe
  README.md "Was der Agent NICHT macht".

## Kontext zum Maintainer

Solo-Entwickler-Projekt (Christian Angermeier, Perculasoft e.K.), Hobby-
Projekt neben Vollzeitstelle. Es gelten keine ISO-Zertifizierungs-Pflichten
— Pragmatismus vor Prozess. Lösungen sollen einem Single-Maintainer-Setup
mit einer Handvoll Kunden entsprechen, nicht Enterprise-Skalierung
vorwegnehmen (kein Kubernetes, kein zentrales Logging-Stack, kein
gestaffeltes Rollout, solange es nicht gebraucht wird).
