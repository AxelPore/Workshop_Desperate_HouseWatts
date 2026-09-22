"""
Simule une coupure de courant réelle sur une zone du réseau en arrêtant
BRUTALEMENT les VMs Proxmox concernées — pas un shutdown propre (ACPI),
mais un "stop" dur (équivalent à débrancher la prise), via l'API Proxmox.

Prérequis :
    pip install proxmoxer requests

Authentification recommandée : un API Token Proxmox (Datacenter > Permissions
> API Tokens), plus fiable qu'un mot de passe et révocable indépendamment.

⚠️ DESTRUCTIF : un "stop" dur coupe la VM comme une vraie coupure de courant
   (pas de fin propre des process, risque de corruption de données non
   sauvegardées). C'est volontaire ici (simulation de crise), mais ne lance
   pas ça sur des VMs de prod sans savoir ce que tu fais.

⚠️ MODE AUTOMATIQUE (appel sans argument) : comme ip_change.py et
   destruction_physique.py, lancer le script sans rien passer tire UNE VM au
   hasard parmi toutes les VMs connues (toutes zones confondues) et la coupe
   directement, SANS demander de confirmation — un appel sans argument est
   par définition non-interactif (ex: appelé par chaos_manager.py, sans
   stdin disponible pour répondre à un prompt).

Usage :
    python power_outage.py                       # ⇦ NOUVEAU : VM aléatoire, coupure auto, sans confirmation
    python power_outage.py --zone quartiers
    python power_outage.py --vm pve-node1 105          # une VM précise
    python power_outage.py --zone serveurs --yes       # sans confirmation (démo/auto)
    python power_outage.py --list                      # lister les zones connues
"""

# argparse : gère les options de la ligne de commande (--zone, --vm, --yes...)
# random   : tirage aléatoire d'une VM en mode automatique (sans argument)
# sys      : utilisé pour sys.exit() (arrêter le script proprement avec un
#            message d'erreur) et sys.stderr (afficher les erreurs séparément
#            de la sortie normale)
import argparse
import random
import sys

# proxmoxer : wrapper Python autour de l'API REST de Proxmox VE. Il transforme
# des appels comme proxmox.nodes(node).qemu(vmid).status.stop.post() en
# requêtes HTTP vers l'API Proxmox, sans avoir à construire l'URL/JSON à la main.
from proxmoxer import ProxmoxAPI

# ---------------------------------------------------------------------------
# 1. Connexion à Proxmox — À ADAPTER à ton environnement
#    Utilise un API Token (Datacenter > Permissions > API Tokens) plutôt
#    qu'un mot de passe : révocable indépendamment du compte, et ne transite
#    pas en clair dans le script si on le charge depuis une variable d'env.
# ---------------------------------------------------------------------------
PROXMOX_HOST = "10.0.0.254"          # IP/hostname du serveur Proxmox (ex: RTR-01/gestion)
PROXMOX_USER = "root@pam"            # ou un user dédié (ex: chaos@pve)
API_TOKEN_NAME = "chaos-script"      # nom du token créé dans Proxmox
API_TOKEN_VALUE = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
VERIFY_SSL = False                   # True si certificat valide en prod, False pour un certif auto-signé de lab

# ---------------------------------------------------------------------------
# 2. Cartographie zone du réseau -> VMs Proxmox concernées
#    (node Proxmox, vmid) — À REMPLIR avec tes vraies VM IDs.
#    C'est cette table qui fait le lien entre le vocabulaire "métier" du
#    workshop (zone "quartiers") et les identifiants techniques Proxmox
#    (quel node héberge la VM, quel est son vmid).
# ---------------------------------------------------------------------------
ZONES = {
    "cockpit": [
        ("pve-node1", 101),   # Poste pilotage
    ],
    "serveurs": [
        ("pve-node1", 102),   # srv-bdd
        ("pve-node1", 103),   # srv-grafana
        ("pve-node1", 104),   # srv-backup
    ],
    "quartiers": [
        ("pve-node1", 105),   # Poste quartier
        ("pve-node1", 106),   # Poste infirmerie
    ],
}

# Liste à plat de toutes les VMs connues (toutes zones confondues), construite
# une seule fois à partir de ZONES. C'est dans cette liste que le mode
# automatique (sans argument) tire une VM au hasard à couper.
ALL_VMS = [(node, vmid) for vms in ZONES.values() for node, vmid in vms]


def connect() -> ProxmoxAPI:
    """Ouvre la connexion à l'API Proxmox et renvoie l'objet client réutilisable
    par toutes les fonctions suivantes (une seule authentification pour tout
    le script, pas une par VM).
    """
    try:
        return ProxmoxAPI(
            PROXMOX_HOST,
            user=PROXMOX_USER,
            token_name=API_TOKEN_NAME,
            token_value=API_TOKEN_VALUE,
            verify_ssl=VERIFY_SSL,
        )
    except Exception as exc:
        # sys.exit() avec un message arrête le script immédiatement et
        # l'affiche comme erreur, plutôt que de laisser une exception brute
        # remonter avec toute la pile d'appels (moins lisible pour la démo)
        sys.exit(f"Connexion à Proxmox impossible : {exc}")


def hard_stop_vm(proxmox: ProxmoxAPI, node: str, vmid: int) -> None:
    """Arrêt dur — équivalent à couper l'alimentation, pas un shutdown ACPI.

    Différence clé avec un arrêt "propre" :
      - status.shutdown.post() envoie un signal ACPI, l'OS de la VM a le temps
        de terminer ses process et de démonter proprement ses disques.
      - status.stop.post()     coupe immédiatement, comme si on débranchait
        la prise : aucune notification à l'OS. C'est ce qu'on veut ici pour
        simuler une vraie coupure de courant (données non enregistrées
        potentiellement perdues, systèmes de fichiers pas démontés proprement).
    """
    try:
        # On vérifie d'abord l'état actuel de la VM avant d'agir, pour éviter
        # une action inutile (et une erreur API) si elle est déjà arrêtée.
        status = proxmox.nodes(node).qemu(vmid).status.current.get()
        current_state = status.get("status", "unknown")
        if current_state != "running":
            print(f"  [SKIP] VM {vmid} sur {node} déjà à l'état '{current_state}'")
            return

        proxmox.nodes(node).qemu(vmid).status.stop.post()
        print(f"  [CUT]  VM {vmid} sur {node} — alimentation coupée brutalement")
    except Exception as exc:
        # On attrape l'exception ici plutôt que de laisser planter tout le
        # script, pour que la coupure des AUTRES VM de la zone continue même
        # si une seule échoue (ex: VM déjà supprimée, node injoignable...)
        print(f"  [ERR]  VM {vmid} sur {node} : {exc}", file=sys.stderr)


def cut_zone(proxmox: ProxmoxAPI, zone: str) -> None:
    """Coupe toutes les VMs associées à une zone métier (ex: 'quartiers')."""
    targets = ZONES.get(zone)
    if not targets:
        sys.exit(f"Zone inconnue : '{zone}'. Zones disponibles : {', '.join(ZONES)}")

    print(f"Coupure de courant simulée sur la zone '{zone}' ({len(targets)} machine(s)) :")
    # On boucle sur chaque VM de la zone et on applique le même traitement
    for node, vmid in targets:
        hard_stop_vm(proxmox, node, vmid)


def cut_single(proxmox: ProxmoxAPI, node: str, vmid: int) -> None:
    """Coupe une seule VM précise, hors notion de zone (utile pour un test ciblé)."""
    print(f"Coupure de courant simulée sur la VM {vmid} (node {node}) :")
    hard_stop_vm(proxmox, node, vmid)


def confirm(message: str) -> bool:
    """Demande une confirmation simple oui/non dans le terminal avant une
    action destructrice, pour éviter un déclenchement accidentel pendant les
    tests (ex: mauvaise commande copiée-collée).
    """
    reply = input(f"{message} [y/N] ").strip().lower()
    return reply == "y"


def main() -> None:
    parser = argparse.ArgumentParser(description="Simule une coupure de courant réelle via arrêt dur de VMs Proxmox.")
    # required=False (au lieu de True) : appeler le script sans aucun
    # argument est désormais un cas valide, traité plus bas comme un tirage
    # aléatoire automatique parmi toutes les VMs connues (ALL_VMS).
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument("--zone", help="Nom de la zone à couper (voir --list)")
    group.add_argument("--vm", nargs=2, metavar=("NODE", "VMID"), help="Cibler une VM précise")
    group.add_argument("--list", action="store_true", help="Lister les zones connues et leurs VMs")
    parser.add_argument("--yes", action="store_true", help="Ne pas demander de confirmation")
    args = parser.parse_args()

    # --list ne touche à aucune VM : on affiche juste la config ZONES et on
    # s'arrête là, pas besoin de se connecter à Proxmox pour ça.
    if args.list:
        for zone, vms in ZONES.items():
            print(f"{zone}:")
            for node, vmid in vms:
                print(f"  - node={node} vmid={vmid}")
        return

    # NOUVEAU : aucun argument fourni (--zone et --vm absents) → mode
    # automatique. On tire une VM au hasard dans ALL_VMS (une seule VM, pas
    # une zone entière, pour rester cohérent avec un "incident ponctuel").
    # Comme ce mode est fait pour tourner sans interaction (ex: appelé par
    # chaos_manager.py sans stdin), on force aussi --yes en interne : pas de
    # prompt de confirmation possible dans ce contexte.
    auto_mode = not args.zone and not args.vm
    if auto_mode:
        node, vmid = random.choice(ALL_VMS)
        args.vm = (node, str(vmid))
        args.yes = True
        print(f"[AUTO] Machine tirée au hasard pour coupure de courant : node={node} vmid={vmid}")

    # Confirmation interactive avant toute action réelle, sauf si --yes est
    # passé (explicitement, ou implicitement en mode auto) — utile pour
    # automatiser la démo en live sans taper "y" à chaque fois.
    if not args.yes:
        target_desc = f"la zone '{args.zone}'" if args.zone else f"la VM {args.vm[1]} sur {args.vm[0]}"
        if not confirm(f"Confirmer la coupure BRUTALE de {target_desc} ?"):
            print("Annulé.")
            return

    # La connexion n'est établie qu'ici, une fois qu'on est sûr qu'une action
    # va réellement avoir lieu (pas de connexion inutile pour --list)
    proxmox = connect()

    if args.zone:
        cut_zone(proxmox, args.zone)
    else:
        node, vmid = args.vm
        cut_single(proxmox, node, int(vmid))


if __name__ == "__main__":
    main()