#!/usr/bin/env python3
"""
Zenico Provisioning Agent
==========================
Pollt Zenico.admin nach Instanzen mit Status "pending" und stellt sie lokal
via Docker Compose bereit. Läuft auf dem Docker-Host selbst (kein SSH).

Eigenständiges Script — Zenico.admin liefert nur Daten, dieses Script macht
die eigentliche Bereitstellung.

Benötigt (siehe requirements-agent.txt): requests, jinja2

Konfiguration über Env-Variablen (eigene .env für den AGENTEN, nicht zu
verwechseln mit der .env, die pro Kunden-Instanz generiert wird):

    ADMIN_API_URL       z.B. https://admin.zenico.app
    ADMIN_API_TOKEN     Bearer-Token für die Agent<->Admin Kommunikation
    DOCKER_IMAGE        z.B. registry.angermeier.net/zenico-app
    INSTANCES_DIR       Basisverzeichnis, z.B. /srv/zenico/instances
    PROXY_NETWORK       Name des externen Docker-Netzwerks für NPM
    POLL_INTERVAL       Sekunden zwischen Polls (Default: 30)
    HEALTH_TIMEOUT       Sekunden bis Health-Check als fehlgeschlagen gilt
"""

import logging
import os
import secrets
import subprocess
import time
from pathlib import Path

import requests
from jinja2 import Environment, FileSystemLoader

ADMIN_API_URL = os.environ["ADMIN_API_URL"].rstrip("/")
ADMIN_API_TOKEN = os.environ["ADMIN_API_TOKEN"]
DOCKER_IMAGE = os.environ.get("DOCKER_IMAGE", "registry.angermeier.net/zenico-app")
INSTANCES_DIR = Path(os.environ.get("INSTANCES_DIR", "/srv/zenico/instances"))
PROXY_NETWORK = os.environ.get("PROXY_NETWORK", "npm_proxy")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "30"))
HEALTH_TIMEOUT = int(os.environ.get("HEALTH_TIMEOUT", "120"))

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


def report_success(instance_id, url):
    session.post(
        f"{ADMIN_API_URL}/api/instances/{instance_id}/complete/",
        json={"url": url},
        timeout=10,
    )


def report_failure(instance_id, message):
    session.post(
        f"{ADMIN_API_URL}/api/instances/{instance_id}/fail/",
        json={"error_message": message},
        timeout=10,
    )


# ---------- Provisioning ----------

def render_templates(instance, target_dir):
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))

    compose_context = {
        "image": DOCKER_IMAGE,
        "image_tag": instance.get("image_tag", "latest"),
        "proxy_network": PROXY_NETWORK,
    }
    compose_tpl = env.get_template("docker-compose.yml.j2")
    (target_dir / "docker-compose.yml").write_text(compose_tpl.render(**compose_context))

    env_context = {
        "secret_key": secrets.token_urlsafe(50),
        "allowed_hosts": instance["subdomain"],
        "db_name": f"zenico_{instance['slug']}",
        "db_user": f"zenico_{instance['slug']}",
        "db_password": secrets.token_urlsafe(32),
        "ki_addon_enabled": instance.get("ki_addon", False),
    }
    env_tpl = env.get_template("env.j2")
    env_path = target_dir / ".env"
    env_path.write_text(env_tpl.render(**env_context))
    env_path.chmod(0o600)


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

    log.info("Provisioniere Instanz %s (%s)", slug, instance["subdomain"])

    render_templates(instance, target_dir)
    docker_compose_up(target_dir)

    if not wait_for_health(slug, HEALTH_TIMEOUT):
        raise RuntimeError(f"Health-Check für {slug} nach {HEALTH_TIMEOUT}s nicht erfolgreich")

    # TODO (separater Schritt/Issue): NPM-Proxy-Host + Let's-Encrypt-Zertifikat
    # automatisch über die NPM-API anlegen. Bis dahin: manuell in NPM eintragen
    # (Forward Hostname: "{slug}-web-1", Forward Port: 8000).

    log.info("Instanz %s erfolgreich provisioniert", slug)
    return f"https://{instance['subdomain']}"


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
                url = provision(instance)
                report_success(instance["id"], url)
            except Exception as exc:
                log.exception("Provisioning fehlgeschlagen für %s", instance.get("slug"))
                report_failure(instance["id"], str(exc))

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main_loop()
