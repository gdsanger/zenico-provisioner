#!/usr/bin/env python3
"""
Zenico Provisioning Agent
==========================
Pollt Zenico.admin nach ungeclaimten Instanzen mit Status "provisioning"
(GET /api/instances/pending/) und stellt sie lokal via Docker Compose
bereit. Läuft auf dem Docker-Host selbst (kein SSH).

Zusätzlich pollt der Agent Instanzen mit Status "deprovisioning"
(GET /api/instances/to-deprovision/) und baut sie vollständig ab (Hard-
Delete: Container + Volumes + Verzeichnis + NPM-Proxy-Host). Ob eine
Instanz dort landet — sofort nach Trial-Ende oder erst nach 30 Tagen
Grace-Period nach einer Kündigung — entscheidet ausschließlich
Zenico.admin; der Agent kennt diesen Unterschied nicht und baut in beiden
Fällen gleich ab.

Eigenständiges Script — Zenico.admin liefert nur Daten, dieses Script macht
die eigentliche Bereitstellung.

Benötigt (siehe requirements-agent.txt): requests, jinja2

Konfiguration über Env-Variablen (eigene .env für den AGENTEN, nicht zu
verwechseln mit der .env, die pro Kunden-Instanz generiert wird):

    ADMIN_API_URL       z.B. https://admin.zenico.app
    ADMIN_API_TOKEN     Bearer-Token für die Agent<->Admin Kommunikation
    DOCKER_IMAGE        z.B. ghcr.io/gdsanger/zenico-app (privates Package —
                        Docker-Host braucht vorher `docker login ghcr.io`,
                        siehe README/CLAUDE.md)
    INSTANCES_DIR       Basisverzeichnis, z.B. /srv/zenico/instances
    PROXY_NETWORK       Name des externen Docker-Netzwerks für NPM
    POLL_INTERVAL       Sekunden zwischen Polls (Default: 30)
    HEALTH_TIMEOUT       Sekunden bis Health-Check als fehlgeschlagen gilt

    INSTANCE_FORWARD_HOST  Von NPM aus erreichbare Adresse dieses
                           Docker-Hosts (IP oder DNS-Name), z.B. wenn NPM
                           auf einem anderen Host läuft als die Instanz
                           (optional, Default leer). Leer = bisheriges
                           Same-Host-Verhalten über den Container-Namen im
                           npm_proxy-Netz. Gesetzt = Instanz veröffentlicht
                           ihren web-Port auf dem Host, NPM leitet an
                           INSTANCE_FORWARD_HOST:<Port> weiter.
    WEB_PORT_BASE          Startwert für die Host-Port-Vergabe im
                           Multi-Host-Modus (Default: 28000). Nur relevant,
                           wenn INSTANCE_FORWARD_HOST gesetzt ist.

    NPM_API_URL         Basis-URL des Nginx Proxy Manager (optional)
    NPM_API_EMAIL       Login-E-Mail des NPM-API-Users (optional)
    NPM_API_PASSWORD    Passwort des NPM-API-Users (optional)

Sind die drei NPM_*-Variablen nicht gesetzt, bleibt das Anlegen des
Proxy-Hosts ein manueller Schritt (nur Log-Hinweis) — wie bisher.
"""

import base64
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
import time
from pathlib import Path

import requests
from jinja2 import Environment, FileSystemLoader

ADMIN_API_URL = os.environ["ADMIN_API_URL"].rstrip("/")
ADMIN_API_TOKEN = os.environ["ADMIN_API_TOKEN"]
DOCKER_IMAGE = os.environ.get("DOCKER_IMAGE", "ghcr.io/gdsanger/zenico-app")
INSTANCES_DIR = Path(os.environ.get("INSTANCES_DIR", "/srv/zenico/instances"))
PROXY_NETWORK = os.environ.get("PROXY_NETWORK", "npm_proxy")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "30"))
HEALTH_TIMEOUT = int(os.environ.get("HEALTH_TIMEOUT", "120"))

# Multi-Host-Forwarding — optional. Leer = Same-Host-Betrieb wie bisher
# (Forward über Container-Namen im npm_proxy-Netz). Gesetzt = die Instanz
# veröffentlicht ihren web-Port auf dem Host, NPM leitet an
# INSTANCE_FORWARD_HOST:<Port> weiter (nötig, sobald NPM und Instanz auf
# unterschiedlichen Hosts laufen und das Docker-Netz nicht mehr trägt).
INSTANCE_FORWARD_HOST = os.environ.get("INSTANCE_FORWARD_HOST", "").strip()
WEB_PORT_BASE = int(os.environ.get("WEB_PORT_BASE", "28000"))

# Nginx Proxy Manager — optional. Nur wenn alle drei gesetzt sind, legt der
# Agent den Proxy-Host automatisch an; sonst bleibt der Schritt manuell.
NPM_API_URL = os.environ.get("NPM_API_URL", "").rstrip("/")
NPM_API_EMAIL = os.environ.get("NPM_API_EMAIL", "")
NPM_API_PASSWORD = os.environ.get("NPM_API_PASSWORD", "")

TEMPLATE_DIR = Path(__file__).parent / "templates"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("provisioner")

session = requests.Session()
session.headers.update({"Authorization": f"Bearer {ADMIN_API_TOKEN}"})


# ---------- Admin-API Kommunikation ----------

def fetch_pending():
    resp = session.get(f"{ADMIN_API_URL}/api/instances/pending/", timeout=10)
    resp.raise_for_status()
    return resp.json()


def claim(instance_id):
    """Markiert die Instanz atomar als 'provisioning' und gibt die vollen
    Daten zurück. 409 = bereits von einem anderen Lauf geclaimt."""
    resp = session.post(f"{ADMIN_API_URL}/api/instances/{instance_id}/claim/", timeout=10)
    if resp.status_code == 409:
        return None
    resp.raise_for_status()
    return resp.json()


def report_success(instance_id, deploy_info):
    """Meldet erfolgreiches Deployment. Payload laut API-CONTRACT.md:
    django_secret_key, db_name, db_user, server_host."""
    resp = session.post(
        f"{ADMIN_API_URL}/api/instances/{instance_id}/complete/",
        json=deploy_info,
        timeout=10,
    )
    resp.raise_for_status()


def report_failure(instance_id, message):
    session.post(
        f"{ADMIN_API_URL}/api/instances/{instance_id}/fail/",
        json={"error_message": message},
        timeout=10,
    )


def fetch_to_deprovision():
    """Analog zu fetch_pending(): Instanzen, die Zenico.admin zum Abbau
    freigegeben hat (Status 'deprovisioning') — egal ob Pfad A (Trial ohne
    Zahlung) oder Pfad B (Kündigung nach 30 Tagen Grace-Period), siehe
    CLAUDE.md."""
    resp = session.get(f"{ADMIN_API_URL}/api/instances/to-deprovision/", timeout=10)
    resp.raise_for_status()
    return resp.json()


def claim_deprovision(instance_id):
    """Markiert die Instanz atomar als 'in Abbau' und gibt die vollen Daten
    zurück. 409 = bereits von einem anderen Lauf geclaimt (Idempotenz,
    analog zu claim())."""
    resp = session.post(f"{ADMIN_API_URL}/api/instances/{instance_id}/claim-deprovision/", timeout=10)
    if resp.status_code == 409:
        return None
    resp.raise_for_status()
    return resp.json()


def report_deprovisioned(instance_id):
    resp = session.post(f"{ADMIN_API_URL}/api/instances/{instance_id}/deprovisioned/", timeout=10)
    resp.raise_for_status()


def report_deprovision_failed(instance_id, message):
    session.post(
        f"{ADMIN_API_URL}/api/instances/{instance_id}/deprovision-failed/",
        json={"error_message": message},
        timeout=10,
    )


# ---------- Nginx Proxy Manager (NPM) ----------

def npm_configured():
    """True, wenn alle drei NPM-Zugangsdaten gesetzt sind."""
    return bool(NPM_API_URL and NPM_API_EMAIL and NPM_API_PASSWORD)


def npm_get_token():
    """Holt ein kurzlebiges NPM-API-Token (POST /api/tokens)."""
    resp = requests.post(
        f"{NPM_API_URL}/api/tokens",
        json={"identity": NPM_API_EMAIL, "secret": NPM_API_PASSWORD},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["token"]


def npm_find_proxy_host(token, fqdn):
    """Sucht einen bestehenden Proxy-Host für diese Domain (Idempotenz).
    Gibt das Host-Objekt zurück oder None. Wichtig für Retry nach 'failed' —
    ein zweiter Lauf darf keinen doppelten Host anlegen."""
    resp = requests.get(
        f"{NPM_API_URL}/api/nginx/proxy-hosts",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    resp.raise_for_status()
    for host in resp.json():
        if fqdn in host.get("domain_names", []):
            return host
    return None


def npm_create_proxy_host(token, fqdn, forward_host, forward_port):
    """Legt einen Proxy-Host mit automatischem Let's-Encrypt-Zertifikat an.
    WebSocket-Support ist aktiviert (HTMX/Live-Updates in Zenico.app)."""
    payload = {
        "domain_names": [fqdn],
        "forward_scheme": "http",
        "forward_host": forward_host,
        "forward_port": forward_port,
        "access_list_id": 0,
        "certificate_id": "new",  # NPM stellt via Let's Encrypt ein neues Zertifikat aus
        "ssl_forced": True,
        "http2_support": True,
        "allow_websocket_upgrade": True,
        "block_exploits": True,
        "caching_enabled": False,
        "hsts_enabled": False,
        "hsts_subdomains": False,
        "advanced_config": "",
        "locations": [],
        "meta": {
            "letsencrypt_email": NPM_API_EMAIL,
            "letsencrypt_agree": True,
            "dns_challenge": False,
        },
    }
    resp = requests.post(
        f"{NPM_API_URL}/api/nginx/proxy-hosts",
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
        timeout=60,  # LE-Zertifikatsausstellung kann einige Sekunden dauern
    )
    resp.raise_for_status()
    return resp.json()


def ensure_proxy_host(instance, web_port):
    """Stellt sicher, dass für die Instanz ein NPM-Proxy-Host mit LE-Zertifikat
    existiert — idempotent (kein doppelter Host bei einem Retry).

    Ist NPM nicht konfiguriert, bleibt der Schritt manuell (nur Log-Hinweis);
    das Verhalten entspricht dem bisherigen Stand. Fehler beim NPM-Aufruf
    werden bewusst NICHT geschluckt, sondern propagiert — so meldet der
    bestehende Pfad in main_loop() die Instanz als 'failed' an Zenico.admin.

    Same-Host (INSTANCE_FORWARD_HOST leer): Forward über den Container-Namen
    im npm_proxy-Netz, wie bisher. Multi-Host (gesetzt): Forward über die
    Host-Adresse + veröffentlichten web-Port, da das Docker-Netz über
    Hostgrenzen hinweg nicht trägt.
    """
    fqdn = instance["fqdn"]
    if INSTANCE_FORWARD_HOST:
        forward_host = INSTANCE_FORWARD_HOST
        forward_port = web_port
    else:
        forward_host = f"{instance['slug']}-web-1"
        forward_port = 8000

    if not npm_configured():
        log.warning(
            "NPM nicht konfiguriert — Proxy-Host für %s bitte manuell anlegen "
            "(Forward Hostname: %s, Forward Port: %s)",
            fqdn, forward_host, forward_port,
        )
        return

    token = npm_get_token()
    if npm_find_proxy_host(token, fqdn):
        log.info("NPM-Proxy-Host für %s existiert bereits, überspringe", fqdn)
        return

    log.info("Lege NPM-Proxy-Host für %s an (Forward: %s:%s)", fqdn, forward_host, forward_port)
    npm_create_proxy_host(token, fqdn, forward_host, forward_port)
    log.info(
        "NPM-Proxy-Host für %s angelegt (Let's-Encrypt-Zertifikat wird ausgestellt)",
        fqdn,
    )


def npm_delete_proxy_host(token, host_id):
    """Entfernt einen Proxy-Host (samt Let's-Encrypt-Zertifikat) über die
    NPM-API."""
    resp = requests.delete(
        f"{NPM_API_URL}/api/nginx/proxy-hosts/{host_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    resp.raise_for_status()


def remove_proxy_host(fqdn):
    """Entfernt den NPM-Proxy-Host für die Domain — idempotent: existiert
    keiner (mehr), z.B. bei einem Retry nach 'deprovision-failed', wird der
    Schritt übersprungen statt einen Fehler zu werfen.

    Ist NPM nicht konfiguriert, bleibt das Entfernen manuell (nur
    Log-Hinweis), analog zu ensure_proxy_host().
    """
    if not npm_configured():
        log.warning(
            "NPM nicht konfiguriert — Proxy-Host für %s bitte manuell entfernen", fqdn,
        )
        return

    token = npm_get_token()
    host = npm_find_proxy_host(token, fqdn)
    if host is None:
        log.info("Kein NPM-Proxy-Host für %s (mehr) vorhanden, überspringe", fqdn)
        return

    log.info("Entferne NPM-Proxy-Host für %s", fqdn)
    npm_delete_proxy_host(token, host["id"])


# ---------- Provisioning ----------

def _existing_env_value(env_path, key):
    """Liest einen einzelnen Wert aus einer bereits vorhandenen .env-Datei,
    ohne die Datei zu parsen/anzufassen (siehe render_templates)."""
    if not env_path.exists():
        return None
    match = re.search(rf"^{re.escape(key)}=(.*)$", env_path.read_text(), re.MULTILINE)
    return match.group(1) if match else None


def allocate_web_port(target_dir):
    """Vergibt einen kollisionsfreien Host-Port für den web-Service im
    Multi-Host-Modus (INSTANCE_FORWARD_HOST gesetzt).

    Vergabe: WEB_PORT_BASE + fortlaufend, kollisionsfrei ermittelt durch
    Scan aller vorhandenen docker-compose.yml-Dateien unter INSTANCES_DIR —
    bewusst ohne eigene Datenbank/State-Datei, passend zum Solo-Maintainer-
    Setup. Existiert für diese Instanz (Retry nach 'failed') bereits ein
    docker-compose.yml mit einem veröffentlichten Port, wird dieser wieder-
    verwendet statt neu vergeben.
    """
    compose_path = target_dir / "docker-compose.yml"
    if compose_path.exists():
        match = re.search(r'"(\d+):8000"', compose_path.read_text())
        if match:
            return int(match.group(1))

    used_ports = set()
    for compose_file in INSTANCES_DIR.glob("*/docker-compose.yml"):
        for match in re.finditer(r'"(\d+):8000"', compose_file.read_text()):
            used_ports.add(int(match.group(1)))

    port = WEB_PORT_BASE
    while port in used_ports:
        port += 1
    return port


def render_templates(instance, target_dir, web_port):
    """Rendert docker-compose.yml und .env für die Instanz.

    Gibt die generierten Deployment-Werte zurück, die anschließend über
    /complete/ an Zenico.admin gemeldet werden (API-CONTRACT.md, Abschnitt 2).
    """
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        trim_blocks=True,
        lstrip_blocks=True,
    )

    compose_context = {
        "image": DOCKER_IMAGE,
        "image_tag": instance.get("image_tag", "latest"),
        "proxy_network": PROXY_NETWORK,
        "web_port": web_port,
    }
    compose_tpl = env.get_template("docker-compose.yml.j2")
    (target_dir / "docker-compose.yml").write_text(compose_tpl.render(**compose_context))

    db_name = f"zenico_{instance['slug']}"
    db_user = f"zenico_{instance['slug']}"
    env_path = target_dir / ".env"

    if env_path.exists():
        # Re-Provisioning (z.B. Retry nach 'failed'): SECRET_KEY,
        # FIELD_ENCRYPTION_KEY und DB_PASSWORD NICHT neu generieren. Sonst
        # sind bereits verschlüsselte DB-Felder (MailConfig/AzureSSOConfig)
        # nicht mehr entschlüsselbar, und der Postgres-Container (persistentes
        # Volume) verweigert mit einem neuen Passwort die Anmeldung.
        log.info("Vorhandene .env für %s gefunden, Secrets bleiben unverändert", instance["slug"])
        secret_key = _existing_env_value(env_path, "SECRET_KEY")
    else:
        secret_key = secrets.token_urlsafe(50)
        # Fernet-Key: exakt 32 Bytes, url-safe base64 MIT Padding.
        # secrets.token_urlsafe() erzeugt KEIN gültiges Fernet-Format (kein Padding).
        field_encryption_key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()

        env_context = {
            "secret_key": secret_key,
            "field_encryption_key": field_encryption_key,
            "allowed_hosts": instance["fqdn"],
            "site_url": f"https://{instance['fqdn']}",
            "db_name": db_name,
            "db_user": db_user,
            "db_password": secrets.token_urlsafe(32),
            "ki_addon_enabled": instance.get("ai_addon_active", False),
            # Phone-Home-Konfiguration (siehe API-CONTRACT.md, Abschnitt 1):
            # damit meldet sich die Kundeninstanz bei Zenico.admin zurück.
            "zenico_admin_url": ADMIN_API_URL,
            "zenico_api_key": instance["api_key"],
            "zenico_instance_id": instance["id"],
            "zenico_customer_id": instance["customer_id"],
        }
        env_tpl = env.get_template("env.j2")
        env_path.write_text(env_tpl.render(**env_context))
        env_path.chmod(0o600)

    return {
        "django_secret_key": secret_key,
        "db_name": db_name,
        "db_user": db_user,
        "server_host": socket.gethostname(),
    }


def docker_compose_up(target_dir):
    subprocess.run(
        ["docker", "compose", "pull"],
        cwd=target_dir, check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["docker", "compose", "up", "-d"],
        cwd=target_dir, check=True, capture_output=True, text=True,
    )


def wait_for_health(slug, timeout):
    """Pollt /healthz/ über `docker exec` im Container selbst — unabhängig
    davon, ob NPM/DNS für die Subdomain schon steht."""
    container = f"{slug}-web-1"  # Docker-Compose-Standardnamensschema (Projektname = Verzeichnisname)
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = subprocess.run(
            ["docker", "exec", container, "curl", "-fsS", "http://localhost:8000/healthz/"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            return True
        time.sleep(3)
    return False


def provision(instance):
    slug = instance["slug"]
    target_dir = INSTANCES_DIR / slug
    target_dir.mkdir(parents=True, exist_ok=True)

    log.info("Provisioniere Instanz %s (%s)", slug, instance["fqdn"])

    web_port = allocate_web_port(target_dir) if INSTANCE_FORWARD_HOST else None
    deploy_info = render_templates(instance, target_dir, web_port)
    docker_compose_up(target_dir)

    if not wait_for_health(slug, HEALTH_TIMEOUT):
        raise RuntimeError(f"Health-Check für {slug} nach {HEALTH_TIMEOUT}s nicht erfolgreich")

    # Proxy-Host + Let's-Encrypt-Zertifikat in NPM anlegen (erst nach dem
    # Health-Check, damit die Domain sofort auf einen laufenden Container zeigt).
    ensure_proxy_host(instance, web_port)

    log.info("Instanz %s erfolgreich provisioniert", slug)
    return deploy_info


# ---------- De-Provisioning ----------

def docker_compose_down(target_dir):
    """Fährt die Instanz-Container herunter und entfernt dabei die Volumes
    (Postgres-/Redis-Daten) — Hard-Delete gemäß CLAUDE.md (DSGVO-Löschung,
    keine Aufbewahrungsfrist auf Docker-Ebene, die läuft ausschließlich als
    Read-Only-Grace-Period vorher in Zenico.admin)."""
    subprocess.run(
        ["docker", "compose", "down", "-v"],
        cwd=target_dir, check=True, capture_output=True, text=True,
    )


def deprovision(instance):
    """Baut eine Instanz vollständig ab: Container + Volumes, NPM-Proxy-Host,
    Verzeichnis. Idempotent — jeder Teilschritt prüft erst, ob es überhaupt
    noch etwas zu entfernen gibt, damit ein Retry nach 'deprovision-failed'
    (z.B. Abbruch mitten im Teardown) keinen Fehler wirft."""
    slug = instance["slug"]
    target_dir = INSTANCES_DIR / slug

    log.info("De-provisioniere Instanz %s (%s)", slug, instance.get("fqdn"))

    if (target_dir / "docker-compose.yml").exists():
        docker_compose_down(target_dir)
    else:
        log.info("Kein docker-compose.yml für %s gefunden, überspringe Container-Teardown", slug)

    fqdn = instance.get("fqdn")
    if fqdn:
        remove_proxy_host(fqdn)

    if target_dir.exists():
        shutil.rmtree(target_dir)

    log.info("Instanz %s vollständig abgebaut", slug)


def main_loop():
    log.info("Provisioning-Agent gestartet. Poll-Intervall: %ss", POLL_INTERVAL)
    while True:
        try:
            pending = fetch_pending()
        except requests.RequestException as exc:
            log.error("Admin-API nicht erreichbar: %s", exc)
            time.sleep(POLL_INTERVAL)
            continue

        for item in pending:
            instance = claim(item["id"])
            if instance is None:
                log.info("Instanz %s bereits von anderem Lauf geclaimt, skip", item["id"])
                continue
            try:
                deploy_info = provision(instance)
                report_success(instance["id"], deploy_info)
            except Exception as exc:
                log.exception("Provisioning fehlgeschlagen für %s", instance.get("slug"))
                report_failure(instance["id"], str(exc))

        try:
            to_deprovision = fetch_to_deprovision()
        except requests.RequestException as exc:
            log.error("Admin-API (to-deprovision) nicht erreichbar: %s", exc)
            time.sleep(POLL_INTERVAL)
            continue

        for item in to_deprovision:
            instance = claim_deprovision(item["id"])
            if instance is None:
                log.info("Instanz %s bereits von anderem Lauf zum Abbau geclaimt, skip", item["id"])
                continue
            try:
                deprovision(instance)
                report_deprovisioned(instance["id"])
            except Exception as exc:
                log.exception("De-Provisioning fehlgeschlagen für %s", instance.get("slug"))
                report_deprovision_failed(instance["id"], str(exc))

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main_loop()
