"""
Healer — reçoit les alertes d'Alertmanager en webhook, décide quelle action
de réparation lancer (Ansible ou Terraform) selon config.yaml, et expose des
métriques Prometheus sur son activité.

Lancer avec :
    uvicorn healer:app --host 0.0.0.0 --port 8000

Tester manuellement sans Alertmanager (utile avant que tes collègues infra
aient fini leurs règles d'alerte) :
    curl -X POST http://localhost:8000/webhook -H "Content-Type: application/json" -d '{
      "alerts": [{
        "status": "firing",
        "labels": {"alertname": "VMDown", "vmid": "105"}
      }]
    }'
"""

import json
import logging
import subprocess
import time
from collections import defaultdict
from pathlib import Path

import yaml
from fastapi import FastAPI, Request
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from starlette.responses import Response

# ---------------------------------------------------------------------------
# Config & logging
# ---------------------------------------------------------------------------
CONFIG_PATH = Path(__file__).parent / "config.yaml"

# Logs au format JSON structuré (une ligne = un événement), pour que Loki ou
# Elasticsearch puissent les parser/filtrer facilement plus tard, plutôt que
# du texte libre difficile à requêter.
logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("healer")


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


config = load_config()
MAX_RETRIES = config.get("max_retries", 3)
ACTIONS = config.get("actions", {})

# ---------------------------------------------------------------------------
# Métriques Prometheus — c'est ce que Grafana viendra afficher.
# Un "Counter" ne fait qu'augmenter (nombre total d'incidents, de succès...).
# Un "Histogram" mesure une distribution (ici : combien de temps prend une
# réparation), ce qui permet d'avoir min/max/moyenne/percentiles dans Grafana.
# Le label "alertname" permet de distinguer les métriques par type d'incident.
# ---------------------------------------------------------------------------
INCIDENTS_TOTAL = Counter("healer_incidents_total", "Nombre d'alertes reçues", ["alertname"])
REPAIRS_SUCCESS = Counter("healer_repairs_success_total", "Réparations réussies", ["alertname"])
REPAIRS_FAILED = Counter("healer_repairs_failed_total", "Réparations échouées", ["alertname"])
ESCALATIONS = Counter("healer_escalations_total", "Alertes escaladées après échecs répétés", ["alertname"])
REPAIR_DURATION = Histogram("healer_repair_duration_seconds", "Durée d'une action de réparation", ["alertname"])

# Compteur d'échecs consécutifs par alerte, en mémoire (clé = alertname+vmid).
# Repart à zéro si le service redémarre — acceptable pour un projet de
# workshop, mais à savoir : en prod on stockerait ça ailleurs (Redis, etc.)
# pour survivre à un redémarrage du healer lui-même.
failure_counts: dict[str, int] = defaultdict(int)

app = FastAPI(title="Chaos Healer")


# ---------------------------------------------------------------------------
# Exécution des actions de réparation
# ---------------------------------------------------------------------------
def run_ansible(action: dict, labels: dict) -> bool:
    """Lance un playbook Ansible avec les labels de l'alerte injectés comme
    extra-vars. Retourne True si ansible-playbook s'est terminé sans erreur.
    """
    playbook = action["playbook"]
    # Les valeurs dans config.yaml contiennent des placeholders style "{vmid}"
    # qu'on remplace ici par les vraies valeurs reçues dans l'alerte.
    extra_vars = {k: v.format(**labels) for k, v in action.get("extra_vars", {}).items()}
    cmd = ["ansible-playbook", playbook, "--extra-vars", json.dumps(extra_vars)]

    logger.info(json.dumps({"event": "run_ansible", "cmd": cmd}))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        # ansible-playbook n'est pas installé/dans le PATH sur cette machine.
        # On traite ça comme un échec de réparation normal (pas un crash du
        # healer) : ça remonte dans les métriques/logs comme n'importe quel
        # autre échec, et peut déclencher l'escalade comme prévu.
        logger.error(json.dumps({"event": "ansible_not_found", "cmd": cmd}))
        return False

    if result.returncode != 0:
        logger.error(json.dumps({
            "event": "ansible_failed",
            "returncode": result.returncode,
            "stderr": result.stderr[-2000:],  # tronqué pour ne pas noyer les logs
        }))
        return False
    return True


def run_terraform(action: dict, labels: dict) -> bool:
    """Lance `terraform apply` dans le dossier configuré, avec les labels de
    l'alerte injectés comme -var. Retourne True si terraform s'est terminé
    sans erreur.
    """
    working_dir = action["working_dir"]
    var_args = []
    for key, value_template in action.get("vars", {}).items():
        var_args += ["-var", f"{key}={value_template.format(**labels)}"]

    cmd = ["terraform", "apply", "-auto-approve"] + var_args
    logger.info(json.dumps({"event": "run_terraform", "cmd": cmd, "cwd": working_dir}))
    try:
        result = subprocess.run(cmd, cwd=working_dir, capture_output=True, text=True)
    except FileNotFoundError:
        logger.error(json.dumps({"event": "terraform_not_found", "cmd": cmd}))
        return False

    if result.returncode != 0:
        logger.error(json.dumps({
            "event": "terraform_failed",
            "returncode": result.returncode,
            "stderr": result.stderr[-2000:],
        }))
        return False
    return True


# Table de dispatch : associe le "type" déclaré dans config.yaml à la
# fonction Python qui sait l'exécuter. Ajouter un nouveau type d'action
# (ex: un script shell custom) ne demande qu'une entrée de plus ici.
ACTION_RUNNERS = {
    "ansible": run_ansible,
    "terraform": run_terraform,
}


# ---------------------------------------------------------------------------
# Logique de décision
# ---------------------------------------------------------------------------
def handle_alert(alertname: str, labels: dict) -> None:
    """Point d'entrée de la logique métier : reçoit un nom d'alerte + ses
    labels, et décide quoi faire.
    """
    INCIDENTS_TOTAL.labels(alertname=alertname).inc()

    action = ACTIONS.get(alertname)
    if not action:
        # Alerte reçue mais pas de règle correspondante dans config.yaml —
        # on log pour que ce soit visible, mais on ne plante pas le service
        # pour autant (d'autres alertes peuvent continuer d'arriver).
        logger.warning(json.dumps({"event": "no_action_configured", "alertname": alertname}))
        return

    # Clé d'escalade : on compte les échecs par COUPLE (alerte, machine),
    # pas juste par type d'alerte — sinon un échec sur la VM 105 bloquerait
    # aussi les tentatives de réparation sur la VM 106.
    key = f"{alertname}:{labels.get('vmid', '')}"

    if failure_counts[key] >= MAX_RETRIES:
        # Trop d'échecs consécutifs : on arrête d'essayer tout seul plutôt
        # que de boucler indéfiniment sur une réparation qui ne marche
        # jamais. C'est le signal "il faut un humain maintenant".
        ESCALATIONS.labels(alertname=alertname).inc()
        logger.error(json.dumps({
            "event": "escalation",
            "alertname": alertname,
            "vmid": labels.get("vmid"),
            "message": f"{MAX_RETRIES} échecs consécutifs — intervention humaine requise",
        }))
        return

    runner = ACTION_RUNNERS.get(action["type"])
    if not runner:
        logger.error(json.dumps({"event": "unknown_action_type", "type": action["type"]}))
        return

    start = time.time()
    success = runner(action, labels)
    duration = time.time() - start
    REPAIR_DURATION.labels(alertname=alertname).observe(duration)

    if success:
        REPAIRS_SUCCESS.labels(alertname=alertname).inc()
        failure_counts[key] = 0  # on repart de zéro après un succès
        logger.info(json.dumps({
            "event": "repair_success", "alertname": alertname,
            "vmid": labels.get("vmid"), "duration_s": round(duration, 2),
        }))
    else:
        REPAIRS_FAILED.labels(alertname=alertname).inc()
        failure_counts[key] += 1
        logger.error(json.dumps({
            "event": "repair_failed", "alertname": alertname,
            "vmid": labels.get("vmid"),
            "attempt": failure_counts[key], "max_retries": MAX_RETRIES,
        }))


# ---------------------------------------------------------------------------
# Routes HTTP
# ---------------------------------------------------------------------------
@app.post("/webhook")
async def webhook(request: Request):
    """Reçoit le payload JSON d'Alertmanager.

    Format standard Alertmanager : {"alerts": [{"status": "firing"/"resolved",
    "labels": {...}, "annotations": {...}, ...}, ...]}
    Une seule requête peut regrouper plusieurs alertes à la fois.
    """
    payload = await request.json()
    handled = 0
    for alert in payload.get("alerts", []):
        if alert.get("status") != "firing":
            # On ne répare que sur les alertes ACTIVES. Un statut "resolved"
            # veut dire que Prometheus considère que le problème est déjà
            # terminé — pas la peine de lancer une réparation dans le vide.
            continue
        labels = alert.get("labels", {})
        alertname = labels.get("alertname", "unknown")
        handle_alert(alertname, labels)
        handled += 1
    return {"received": len(payload.get("alerts", [])), "handled": handled}


@app.get("/metrics")
def metrics():
    """Endpoint que Prometheus vient scraper pour récupérer les compteurs
    définis plus haut (healer_incidents_total, etc.), au format texte
    Prometheus standard.
    """
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/health")
def health():
    """Simple endpoint de vérification que le service tourne — pratique pour
    un check Prometheus (up{job="healer"}) ou juste un test manuel rapide.
    """
    return {"status": "ok"}