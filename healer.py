"""
Healer — reçoit les alertes d'Alertmanager en webhook, décide quelle action
de réparation lancer (Ansible ou Terraform) selon config.yaml, et expose des
métriques Prometheus sur son activité.

Lancer avec :
    uvicorn healer:app --host 0.0.0.0 --port 8000

Tester manuellement sans Alertmanager (utile avant que tes collègues infra
aient fini leurs règles d'alerte) :
    curl -X POST http://localhost:8000/webhook \
      -H "Content-Type: application/json" \
      -H "X-Webhook-Secret: change-me" \
      -d '{
        "alerts": [{
          "status": "firing",
          "labels": {"alertname": "VMDown", "vmid": "105"}
        }]
      }'

Sécurité : le endpoint /webhook attend un header X-Webhook-Secret qui doit
correspondre à la valeur "webhook_secret" de config.yaml. Configure la même
valeur côté Alertmanager (webhook_configs -> http_config -> headers).
"""

import json
import logging
import subprocess
import threading
import time
from collections import defaultdict
from pathlib import Path

import yaml
from fastapi import FastAPI, Request, Response as FastAPIResponse
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from starlette.responses import Response

# ---------------------------------------------------------------------------
# Config & logging
# ---------------------------------------------------------------------------
CONFIG_PATH = Path(__file__).parent / "config.yaml"
STATE_PATH = Path(__file__).parent / "healer_state.json"

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
# Timeout par défaut (secondes) pour chaque subprocess (ansible/terraform),
# surchargeable par action via "timeout_seconds" dans config.yaml.
DEFAULT_TIMEOUT = config.get("default_timeout_seconds", 300)
# Secret attendu dans le header X-Webhook-Secret. Si absent de config.yaml,
# l'auth est désactivée (utile en dev) mais un warning est loggé au démarrage.
WEBHOOK_SECRET = config.get("webhook_secret")

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
WEBHOOK_REJECTED = Counter("healer_webhook_rejected_total", "Requêtes webhook rejetées (auth invalide)")

# ---------------------------------------------------------------------------
# État : compteur d'échecs consécutifs par alerte (clé = alertname+vmid).
#
# Persisté dans un petit fichier JSON à côté du script, pour survivre à un
# redémarrage du healer. Ce n'est pas fait pour tenir plusieurs instances du
# healer en parallèle (pas de verrou inter-processus) — pour ça il faudrait
# un backend partagé type Redis — mais ça couvre largement le cas "un seul
# healer, qui peut redémarrer" d'un workshop ou d'un petit déploiement.
# ---------------------------------------------------------------------------
_state_lock = threading.Lock()  # protège les accès à failure_counts + le fichier
failure_counts: dict[str, int] = defaultdict(int)

# Un verrou par clé (alertname:vmid) pour empêcher deux réparations
# concurrentes sur la même cible (ex: deux alertes VMDown/vmid=105 qui
# arrivent presque en même temps dans deux requêtes webhook différentes).
_key_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
_key_locks_guard = threading.Lock()  # protège la création des Lock() ci-dessus


def _get_key_lock(key: str) -> threading.Lock:
    with _key_locks_guard:
        return _key_locks[key]


def load_state() -> None:
    if not STATE_PATH.exists():
        return
    try:
        with open(STATE_PATH) as f:
            data = json.load(f)
        with _state_lock:
            failure_counts.update(data)
        logger.info(json.dumps({"event": "state_loaded", "entries": len(data)}))
    except (json.JSONDecodeError, OSError) as e:
        # Un state.json corrompu ne doit pas empêcher le healer de démarrer —
        # on repart juste avec des compteurs à zéro.
        logger.error(json.dumps({"event": "state_load_failed", "error": str(e)}))


def save_state() -> None:
    try:
        with _state_lock:
            snapshot = dict(failure_counts)
        tmp_path = STATE_PATH.with_suffix(".tmp")
        with open(tmp_path, "w") as f:
            json.dump(snapshot, f)
        tmp_path.replace(STATE_PATH)  # écriture atomique
    except OSError as e:
        logger.error(json.dumps({"event": "state_save_failed", "error": str(e)}))


load_state()

if not WEBHOOK_SECRET:
    logger.warning(json.dumps({
        "event": "webhook_auth_disabled",
        "message": "Aucun 'webhook_secret' dans config.yaml — /webhook accepte toute requête. "
                   "À ne pas utiliser tel quel en dehors d'un réseau de confiance/dev.",
    }))

app = FastAPI(title="Chaos Healer")


# ---------------------------------------------------------------------------
# Exécution des actions de réparation
# ---------------------------------------------------------------------------
def run_ansible(action: dict, labels: dict) -> bool:
    """Lance un playbook Ansible avec les labels de l'alerte injectés comme
    extra-vars. Retourne True si ansible-playbook s'est terminé sans erreur.
    """
    playbook = action["playbook"]
    timeout = action.get("timeout_seconds", DEFAULT_TIMEOUT)

    # Les valeurs dans config.yaml contiennent des placeholders style "{vmid}"
    # qu'on remplace ici par les vraies valeurs reçues dans l'alerte.
    try:
        extra_vars = {k: v.format(**labels) for k, v in action.get("extra_vars", {}).items()}
    except KeyError as e:
        # Un placeholder référence un label absent de l'alerte reçue
        # (ex: "{service}" configuré mais l'alerte n'a pas de label
        # "service"). On traite ça comme un échec de réparation classique
        # plutôt que de laisser planter tout le traitement du batch.
        logger.error(json.dumps({
            "event": "template_error", "action": "ansible",
            "playbook": playbook, "missing_label": str(e),
        }))
        return False

    cmd = ["ansible-playbook", playbook, "--extra-vars", json.dumps(extra_vars)]

    logger.info(json.dumps({"event": "run_ansible", "cmd": cmd, "timeout_s": timeout}))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        # ansible-playbook n'est pas installé/dans le PATH sur cette machine.
        # On traite ça comme un échec de réparation normal (pas un crash du
        # healer) : ça remonte dans les métriques/logs comme n'importe quel
        # autre échec, et peut déclencher l'escalade comme prévu.
        logger.error(json.dumps({"event": "ansible_not_found", "cmd": cmd}))
        return False
    except subprocess.TimeoutExpired:
        logger.error(json.dumps({"event": "ansible_timeout", "cmd": cmd, "timeout_s": timeout}))
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

    Un `terraform plan` est loggé avant l'`apply`, uniquement à titre de
    traçabilité (visible dans les logs) — ça ne bloque pas l'apply si le
    plan échoue lui-même à s'exécuter pour une raison indépendante.
    """
    working_dir = action["working_dir"]
    timeout = action.get("timeout_seconds", DEFAULT_TIMEOUT)

    try:
        var_args = []
        for key, value_template in action.get("vars", {}).items():
            var_args += ["-var", f"{key}={value_template.format(**labels)}"]
    except KeyError as e:
        logger.error(json.dumps({
            "event": "template_error", "action": "terraform",
            "working_dir": working_dir, "missing_label": str(e),
        }))
        return False

    # Plan (best-effort, pour la trace) — on n'échoue pas l'action si cette
    # étape a un souci d'exécution, seul l'apply compte pour le résultat.
    try:
        plan_result = subprocess.run(
            ["terraform", "plan"] + var_args,
            cwd=working_dir, capture_output=True, text=True, timeout=timeout,
        )
        logger.info(json.dumps({
            "event": "terraform_plan",
            "returncode": plan_result.returncode,
            "stdout_tail": plan_result.stdout[-2000:],
        }))
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.warning(json.dumps({"event": "terraform_plan_skipped", "reason": str(e)}))

    cmd = ["terraform", "apply", "-auto-approve"] + var_args
    logger.info(json.dumps({"event": "run_terraform", "cmd": cmd, "cwd": working_dir, "timeout_s": timeout}))
    try:
        result = subprocess.run(cmd, cwd=working_dir, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        logger.error(json.dumps({"event": "terraform_not_found", "cmd": cmd}))
        return False
    except subprocess.TimeoutExpired:
        logger.error(json.dumps({"event": "terraform_timeout", "cmd": cmd, "timeout_s": timeout}))
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

    # Un seul repair à la fois par clé : si une réparation sur cette même
    # cible est déjà en cours (ex: deux alertes quasi simultanées), la
    # deuxième attend son tour plutôt que de se lancer en parallèle.
    lock = _get_key_lock(key)
    with lock:
        with _state_lock:
            current_failures = failure_counts[key]

        if current_failures >= MAX_RETRIES:
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

        with _state_lock:
            if success:
                failure_counts[key] = 0  # on repart de zéro après un succès
            else:
                failure_counts[key] += 1
            attempt = failure_counts[key]
        save_state()

        if success:
            REPAIRS_SUCCESS.labels(alertname=alertname).inc()
            logger.info(json.dumps({
                "event": "repair_success", "alertname": alertname,
                "vmid": labels.get("vmid"), "duration_s": round(duration, 2),
            }))
        else:
            REPAIRS_FAILED.labels(alertname=alertname).inc()
            logger.error(json.dumps({
                "event": "repair_failed", "alertname": alertname,
                "vmid": labels.get("vmid"),
                "attempt": attempt, "max_retries": MAX_RETRIES,
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

    Si "webhook_secret" est défini dans config.yaml, la requête doit porter
    un header "X-Webhook-Secret" avec la même valeur, sinon elle est rejetée
    (401). Configure Alertmanager pour envoyer ce header (http_config ->
    headers dans le webhook_config).
    """
    if WEBHOOK_SECRET:
        provided = request.headers.get("x-webhook-secret")
        if provided != WEBHOOK_SECRET:
            WEBHOOK_REJECTED.inc()
            logger.warning(json.dumps({"event": "webhook_rejected", "reason": "invalid_secret"}))
            return FastAPIResponse(
                content=json.dumps({"error": "unauthorized"}),
                status_code=401,
                media_type="application/json",
            )

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