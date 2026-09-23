"""
Simule des DÉGÂTS PHYSIQUES sur une machine (ex: impact micrométéorite) en
détruisant réellement la VM Proxmox correspondante — pas un arrêt, une
suppression complète (config + disques). Irréversible, sauf si tu choisis
l'option de sauvegarde préalable.

Prérequis :
    pip install proxmoxer requests

⚠️ IRRÉVERSIBLE PAR DÉFAUT. Contrairement à power_outage.py (arrêt dur,
   la VM peut être rallumée), ce script SUPPRIME la VM. Utilise
   --backup-before si tu veux pouvoir la restaurer entre deux répétitions
   de ta démo, sinon prépare-toi à recréer la VM depuis un template/clone.

⚠️ MODE AUTOMATIQUE (appel sans argument) : la confirmation interactive
   ("Tape DESTROY...") est SAUTÉE, car un script lancé sans argument est
   par définition non-interactif (ex: appelé par chaos_manager.py). La VM
   est réellement détruite sans demander confirmation. Utilise --vm ou
   --zone en manuel si tu veux garder le filet de sécurité du prompt.

Usage :
    python destroy_vm.py                       # ⇦ NOUVEAU : VM aléatoire, destruction auto, sans prompt
    python destroy_vm.py --vm pve-node1 105
    python destroy_vm.py --vm pve-node1 105 --backup-before
    python destroy_vm.py --zone quartiers --confirm DESTROY
"""

# argparse : options de ligne de commande
# random   : tirage aléatoire d'une VM en mode automatique (sans argument)
# sys      : sys.exit() / sys.stderr pour les erreurs
# time     : time.sleep() pour laisser Proxmox le temps de traiter les actions
#            asynchrones (backup, arrêt) avant d'enchaîner l'étape suivante
import argparse
import random
import sys
import time

from proxmoxer import ProxmoxAPI  # client API Proxmox, voir power_outage.py pour le détail

# ---------------------------------------------------------------------------
# 1. Connexion à Proxmox — mêmes identifiants que power_outage.py
# ---------------------------------------------------------------------------
PROXMOX_HOST = "10.0.0.254"
PROXMOX_USER = "root@pam"
API_TOKEN_NAME = "chaos-script"
API_TOKEN_VALUE = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
VERIFY_SSL = False

# Stockage utilisé pour les sauvegardes préalables (--backup-before)
BACKUP_STORAGE = "local"

# ---------------------------------------------------------------------------
# 2. Cartographie zone -> VMs (identique à power_outage.py)
# ---------------------------------------------------------------------------
ZONES = {
    "cockpit":   [("pve-node1", 110)],
    "serveurs":  [("pve-node1", 101), ("pve-node1", 103)],
    "quartiers": [("pve-node1", 130), ("pve-node1", 120)],
}

# Liste à plat de toutes les VMs connues (toutes zones confondues), construite
# une seule fois à partir de ZONES. C'est dans cette liste que le mode
# automatique (sans argument) tire une VM au hasard.
ALL_VMS = [(node, vmid) for vms in ZONES.values() for node, vmid in vms]


def connect() -> ProxmoxAPI:
    """Identique à power_outage.py : une seule authentification réutilisée
    pour toutes les opérations du script."""
    try:
        return ProxmoxAPI(
            PROXMOX_HOST, user=PROXMOX_USER,
            token_name=API_TOKEN_NAME, token_value=API_TOKEN_VALUE,
            verify_ssl=VERIFY_SSL,
        )
    except Exception as exc:
        sys.exit(f"Connexion à Proxmox impossible : {exc}")


def wait_for_task(proxmox: ProxmoxAPI, node: str, upid: str, timeout: int = 120) -> None:
    """Attend qu'une tâche asynchrone Proxmox (backup, delete...) se termine.

    Beaucoup d'actions Proxmox (comme vzdump) ne sont PAS instantanées : l'API
    répond tout de suite avec un identifiant de tâche (UPID) et le travail
    continue en arrière-plan côté serveur. Cette fonction interroge
    régulièrement (polling) le statut de cette tâche jusqu'à ce qu'elle soit
    marquée "stopped", pour être sûr que la sauvegarde est vraiment terminée
    avant de passer à la destruction de la VM.
    """
    start = time.time()
    while time.time() - start < timeout:
        status = proxmox.nodes(node).tasks(upid).status.get()
        if status.get("status") == "stopped":
            if status.get("exitstatus") != "OK":
                raise RuntimeError(f"Tâche {upid} échouée : {status.get('exitstatus')}")
            return
        time.sleep(2)  # on ne martèle pas l'API, on vérifie toutes les 2s
    raise TimeoutError(f"Tâche {upid} non terminée après {timeout}s")


def backup_vm(proxmox: ProxmoxAPI, node: str, vmid: int) -> None:
    """Lance une sauvegarde (vzdump) de la VM avant destruction, pour pouvoir
    la restaurer entre deux répétitions de la démo. mode="snapshot" permet de
    sauvegarder sans arrêter la VM au préalable (moins perturbant pour un test).
    """
    print(f"  [BACKUP] Sauvegarde de la VM {vmid} avant destruction...")
    upid = proxmox.nodes(node).vzdump.post(vmid=vmid, storage=BACKUP_STORAGE, mode="snapshot")
    wait_for_task(proxmox, node, upid)  # on attend la fin réelle du backup avant de continuer
    print(f"  [BACKUP] Sauvegarde terminée (storage: {BACKUP_STORAGE})")


def destroy_vm(proxmox: ProxmoxAPI, node: str, vmid: int, backup_before: bool) -> None:
    """Détruit réellement une VM : sauvegarde optionnelle, arrêt si besoin,
    puis suppression complète de sa config et de ses disques.
    """
    try:
        # On vérifie d'abord que la VM existe et récupère son état courant
        status = proxmox.nodes(node).qemu(vmid).status.current.get()
    except Exception as exc:
        print(f"  [ERR]  VM {vmid} sur {node} introuvable : {exc}", file=sys.stderr)
        return

    if backup_before:
        backup_vm(proxmox, node, vmid)

    # Proxmox refuse de supprimer une VM encore en cours d'exécution : il faut
    # d'abord l'arrêter (arrêt dur, cohérent avec l'idée de dégâts physiques —
    # on ne demande pas gentiment à l'OS de s'éteindre avant de le détruire)
    if status.get("status") == "running":
        print(f"  [STOP]  VM {vmid} en cours d'arrêt forcé avant destruction...")
        proxmox.nodes(node).qemu(vmid).status.stop.post()
        time.sleep(3)  # laisse le temps à Proxmox de finaliser l'arrêt côté hyperviseur

    try:
        # delete(purge=1) : supprime la VM ET ses traces dans les jobs de
        # sauvegarde/replication existants. Sans purge=1, la VM disparaît mais
        # peut laisser des références orphelines dans la config Proxmox.
        proxmox.nodes(node).qemu(vmid).delete(purge=1)
        print(f"  [DESTROY] VM {vmid} sur {node} — détruite (config + disques supprimés)")
    except Exception as exc:
        print(f"  [ERR]  Échec de la destruction de la VM {vmid} : {exc}", file=sys.stderr)


def destroy_zone(proxmox: ProxmoxAPI, zone: str, backup_before: bool) -> None:
    """Applique destroy_vm() à toutes les VMs d'une zone métier."""
    targets = ZONES.get(zone)
    if not targets:
        sys.exit(f"Zone inconnue : '{zone}'. Zones disponibles : {', '.join(ZONES)}")

    print(f"Dégâts physiques simulés sur la zone '{zone}' ({len(targets)} machine(s)) :")
    for node, vmid in targets:
        destroy_vm(proxmox, node, vmid, backup_before)


def main() -> None:
    parser = argparse.ArgumentParser(description="Simule des dégâts physiques : détruit réellement une/des VM Proxmox.")
    # required=False (au lieu de True) : appeler le script sans aucun
    # argument est désormais un cas valide, traité plus bas comme un tirage
    # aléatoire automatique parmi toutes les VMs connues (ALL_VMS).
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument("--vm", nargs=2, metavar=("NODE", "VMID"), help="Détruire une VM précise")
    group.add_argument("--zone", help="Détruire toutes les VMs d'une zone")
    group.add_argument("--list", action="store_true", help="Lister les zones connues et leurs VMs")

    parser.add_argument("--backup-before", action="store_true",
                         help="Sauvegarder la VM (vzdump) avant destruction, pour pouvoir la restaurer")
    # --confirm DESTROY permet de scripter l'appel (ex: dans une démo
    # automatisée) sans passer par l'input() interactif ci-dessous.
    parser.add_argument("--confirm", metavar="DESTROY",
                         help="Doit valoir exactement DESTROY pour confirmer sans prompt interactif (ex: script automatisé)")
    args = parser.parse_args()

    if args.list:
        for zone, vms in ZONES.items():
            print(f"{zone}:")
            for node, vmid in vms:
                print(f"  - node={node} vmid={vmid}")
        return

    # NOUVEAU : aucun argument fourni (--vm et --zone absents) → mode
    # automatique. On tire une VM au hasard dans ALL_VMS, et comme ce mode
    # est fait pour être appelé sans interaction (ex: chaos_manager.py qui
    # ne fournit pas de stdin), la confirmation "DESTROY" est sautée plus
    # bas via ce flag auto_mode.
    auto_mode = not args.vm and not args.zone
    if auto_mode:
        node, vmid = random.choice(ALL_VMS)
        args.vm = (node, str(vmid))
        print(f"[AUTO] Machine tirée au hasard pour destruction : node={node} vmid={vmid}")

    target_desc = f"la zone '{args.zone}'" if args.zone else f"la VM {args.vm[1]} sur {args.vm[0]}"

    # Confirmation renforcée par rapport à power_outage.py : on exige de
    # taper le mot "DESTROY" en entier plutôt qu'un simple y/N, car l'action
    # est irréversible (contrairement à un arrêt, qu'on peut annuler en
    # rallumant la VM). En mode auto, on saute ce prompt : il n'y a personne
    # pour répondre, et c'est justement l'objectif du mode automatique.
    if not auto_mode and args.confirm != "DESTROY":
        print(f"Cette action va DÉTRUIRE DÉFINITIVEMENT {target_desc}.")
        typed = input("Tape DESTROY en majuscules pour confirmer : ").strip()
        if typed != "DESTROY":
            print("Annulé.")
            return
    elif auto_mode:
        print(f"[AUTO] Confirmation automatique (mode non-interactif) — destruction de {target_desc} sans prompt.")

    proxmox = connect()

    if args.zone:
        destroy_zone(proxmox, args.zone, args.backup_before)
    else:
        node, vmid = args.vm
        destroy_vm(proxmox, node, int(vmid), args.backup_before)


if __name__ == "__main__":
    main()