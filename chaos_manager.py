"""
Chaos Manager
=============
Lance aléatoirement un des trois scripts de chaos toutes les 1 à 5 minutes.

Pondération :
    - Au départ, les 3 scripts ont une probabilité égale d'être tirés.
    - Chaque fois qu'un script est lancé, son "poids" diminue, ce qui réduit
      sa probabilité d'être retiré la prochaine fois (pour éviter qu'un
      même script ne monopolise le chaos plusieurs fois de suite).
    - Poids d'un script = 1 / (1 + nb_de_lancements_déjà_effectués)
      → 1er tirage : poids 1.0 (identique pour les 3)
      → après 1 lancement : poids 0.5
      → après 2 lancements : poids 0.33, etc.

Prérequis :
    - destruction_physique.py, electricity_cut.py et ip_change.py doivent
      se trouver dans le même dossier que ce script (ou adapter SCRIPTS_DIR
      ci-dessous).

Usage :
    python chaos_manager.py
    python chaos_manager.py --min-interval 60 --max-interval 300
    python chaos_manager.py --once          # un seul tirage puis on quitte
    python chaos_manager.py --dry-run       # affiche les tirages sans exécuter les scripts
"""

import argparse
import random
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration — À ADAPTER si besoin
# ---------------------------------------------------------------------------
SCRIPTS_DIR = Path(__file__).resolve().parent
SCRIPTS = [
    "destruction_physique.py",
    "electricity_cut.py",
    "ip_change.py",
]

PYTHON_EXE = sys.executable  # utilise le même interpréteur que celui qui lance chaos_manager.py


# ---------------------------------------------------------------------------
# Utilitaire d'affichage
# ---------------------------------------------------------------------------
def log(msg: str) -> None:
    """Affiche un message précédé d'un horodatage, pour suivre l'activité
    du Chaos Manager dans les logs/la console."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}")


# ---------------------------------------------------------------------------
# Logique de pondération / tirage aléatoire
# ---------------------------------------------------------------------------
def compute_weights(run_counts: dict) -> list:
    """Calcule le poids actuel de chaque script, dans le même ordre que SCRIPTS.

    Formule : poids = 1 / (1 + nb de lancements déjà effectués)
        - Un script jamais lancé (run_counts = 0) a un poids de 1.0.
        - Après 1 lancement, son poids tombe à 0.5 (deux fois moins probable).
        - Après 2 lancements, 0.33, etc.
    Plus un script a déjà été tiré, plus son poids (donc sa probabilité
    relative d'être retiré) diminue par rapport aux autres.
    """
    return [1 / (1 + run_counts[script]) for script in SCRIPTS]


def pick_script(run_counts: dict) -> str:
    """Tire un script au hasard parmi SCRIPTS, en tenant compte des poids
    actuels (random.choices fait un tirage pondéré, pas un choix uniforme)."""
    weights = compute_weights(run_counts)
    chosen = random.choices(SCRIPTS, weights=weights, k=1)[0]
    return chosen


# ---------------------------------------------------------------------------
# Exécution effective d'un script de chaos
# ---------------------------------------------------------------------------
def run_script(script_name: str, dry_run: bool) -> None:
    """Exécute le script choisi comme un sous-processus Python indépendant.

    - En mode --dry-run : on se contente d'afficher ce qui AURAIT été lancé,
      sans toucher au réseau/à l'infra (utile pour tester le manager seul).
    - Sinon : on vérifie d'abord que le fichier existe, puis on le lance
      avec subprocess.run (appel BLOQUANT : le manager attend la fin du
      script avant de continuer, donc pas d'exécution en parallèle).
    - stdout/stderr du script enfant sont récupérés et réaffichés ici pour
      garder toute la sortie dans un seul flux de logs.
    """
    script_path = SCRIPTS_DIR / script_name

    # Mode simulation : on ne lance rien, on log juste l'intention.
    if dry_run:
        log(f"[DRY-RUN] Aurait lancé : {script_path}")
        return

    # Sécurité : si le fichier n'existe pas (mauvais dossier, typo...),
    # on ne plante pas le manager, on saute juste ce tirage.
    if not script_path.exists():
        log(f"[ERREUR] Script introuvable : {script_path} — tirage ignoré.")
        return

    log(f"Lancement de {script_name} ...")
    try:
        # Lance `python3 script.py` et attend qu'il se termine.
        result = subprocess.run(
            [PYTHON_EXE, str(script_path)],
            capture_output=True,  # capture stdout/stderr au lieu de les laisser s'afficher directement
            text=True,            # récupère du texte plutôt que des bytes
        )
        if result.stdout.strip():
            print(result.stdout.strip())

        # Un code de retour différent de 0 = le script a rencontré une erreur.
        if result.returncode != 0:
            log(f"[ATTENTION] {script_name} a terminé avec le code {result.returncode}")
            if result.stderr.strip():
                print(result.stderr.strip(), file=sys.stderr)
        else:
            log(f"{script_name} terminé avec succès.")
    except Exception as e:
        # Filet de sécurité général (ex: interpréteur introuvable, permissions...)
        log(f"[ERREUR] Échec du lancement de {script_name} : {e}")


# ---------------------------------------------------------------------------
# Point d'entrée principal
# ---------------------------------------------------------------------------
def main() -> None:
    # --- Lecture des options en ligne de commande ---
    parser = argparse.ArgumentParser(description="Lance aléatoirement des scripts de chaos, pondération décroissante.")
    parser.add_argument("--min-interval", type=int, default=60, help="Intervalle minimum en secondes (défaut 60 = 1 min)")
    parser.add_argument("--max-interval", type=int, default=300, help="Intervalle maximum en secondes (défaut 300 = 5 min)")
    parser.add_argument("--once", action="store_true", help="Ne fait qu'un seul tirage puis quitte")
    parser.add_argument("--dry-run", action="store_true", help="Simule les tirages sans exécuter les scripts")
    args = parser.parse_args()

    # Compteur de lancements par script : c'est ce dictionnaire qui pilote
    # la pondération décroissante (voir compute_weights). Tous à 0 au départ
    # → première itération avec des chances égales pour les 3 scripts.
    run_counts = {script: 0 for script in SCRIPTS}

    log("=== Chaos Manager démarré ===")
    log(f"Scripts en jeu : {', '.join(SCRIPTS)}")
    log(f"Intervalle : {args.min_interval}s – {args.max_interval}s")
    if args.dry_run:
        log("Mode DRY-RUN activé : aucun script ne sera réellement exécuté.")

    try:
        # Boucle infinie : tirage → exécution → pause → on recommence.
        # S'arrête uniquement avec --once, Ctrl+C, ou un kill du process.
        while True:
            # 1. Tirage pondéré du prochain script à exécuter.
            chosen = pick_script(run_counts)
            weights = compute_weights(run_counts)
            log(f"Poids actuels : {dict(zip(SCRIPTS, [round(w, 3) for w in weights]))}")
            log(f"→ Tirage : {chosen}")

            # 2. Exécution du script tiré, puis mise à jour de son compteur
            #    (c'est cette incrémentation qui fait baisser son poids
            #    pour le prochain tirage).
            run_script(chosen, args.dry_run)
            run_counts[chosen] += 1

            # 3. Si on est en mode "un seul tirage", on s'arrête ici.
            if args.once:
                log("Mode --once : arrêt après un tirage.")
                break

            # 4. Pause aléatoire entre 1 et 5 minutes (bornes configurables)
            #    avant le tirage suivant, pour un chaos imprévisible dans
            #    le temps ET dans le choix du script.
            wait_time = random.randint(args.min_interval, args.max_interval)
            log(f"Prochain tirage dans {wait_time} secondes ({wait_time // 60}m{wait_time % 60:02d}s)...")
            time.sleep(wait_time)

    except KeyboardInterrupt:
        # Permet d'arrêter proprement le manager avec Ctrl+C plutôt que
        # de laisser une trace d'erreur brute, et affiche un bilan final.
        log("Interruption manuelle (Ctrl+C). Arrêt du Chaos Manager.")
        log(f"Récapitulatif des lancements : {run_counts}")


if __name__ == "__main__":
    main()