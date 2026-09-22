"""
Change l'IP d'une machine Linux du réseau, tirée au sort (ou choisie) dans une
liste connue, en se connectant dessus en SSH depuis ce poste central.

Prérequis :
    - Accès SSH par clé (sans mot de passe) configuré vers chaque machine
      de MACHINES, avec un compte ayant les droits sudo sans mot de passe
      pour la commande `ip` (sinon le script restera bloqué sur le prompt
      de mot de passe sudo, qu'il ne peut pas taper à ta place).

⚠️ RISQUE DE COUPURE DE LA SESSION SSH ELLE-MÊME : si l'interface changée
   est celle par laquelle transite la connexion SSH, la session va sembler
   se figer / se couper au moment du `ip addr flush`. C'est attendu : les
   commandes suivantes (add, link up) continuent de s'exécuter côté distant
   même si tu ne vois plus la sortie côté client. Vérifie ensuite l'état
   réel via un nouveau SSH vers la nouvelle IP.

Usage :
    python change_ip_fleet.py                     # ⇦ NOUVEAU : équivaut à --random
    python change_ip_fleet.py --list
    python change_ip_fleet.py --random
    python change_ip_fleet.py --machine "srv-bdd"
    python change_ip_fleet.py --machine "srv-bdd" --new-ip 172.16.0.42
"""

import argparse
import ipaddress
import random
import subprocess
import sys

# ---------------------------------------------------------------------------
# 1. Configuration SSH — À ADAPTER
# ---------------------------------------------------------------------------
SSH_USER = "axel"  # compte utilisé pour se connecter à chaque machine

# ---------------------------------------------------------------------------
# 2. Liste des machines du réseau — À REMPLIR avec tes vraies IP/interfaces
#    "host"      : IP actuelle utilisée pour se connecter en SSH
#    "interface" : nom de l'interface réseau sur CETTE machine (vérifier
#                  avec `ip link show` en te connectant dessus une première fois)
#    "cidr"      : masque en notation CIDR du sous-réseau de cette machine
# ---------------------------------------------------------------------------
MACHINES = [
    {"name": "Poste pilotage",   "host": "10.0.0.10",    "interface": "eth0", "cidr": 24},
    {"name": "Poste infirmerie", "host": "10.0.0.20",    "interface": "eth0", "cidr": 24},
    {"name": "Poste quartier",   "host": "10.0.0.30",    "interface": "eth0", "cidr": 24},
    {"name": "srv-bdd",          "host": "172.16.0.251", "interface": "eth0", "cidr": 24},
    {"name": "srv-grafana",      "host": "172.16.0.250", "interface": "eth0", "cidr": 24},
    {"name": "srv-backup",       "host": "172.16.0.200", "interface": "eth0", "cidr": 24},
]


def pick_new_ip(current_host: str, cidr: int) -> str:
    """Tire une IP libre au hasard dans le même sous-réseau que la machine.

    On exclut l'IP actuelle de la machine (pas de changement pour rien) et
    les IP déjà utilisées par une autre machine de MACHINES, pour éviter un
    conflit IP si jamais elles sont sur le même sous-réseau.
    """
    network = ipaddress.ip_network(f"{current_host}/{cidr}", strict=False)
    used_ips = {m["host"] for m in MACHINES}
    candidates = [str(ip) for ip in network.hosts() if str(ip) not in used_ips]
    if not candidates:
        sys.exit(f"Aucune IP libre trouvée dans {network}")
    return random.choice(candidates)


def run_remote(host: str, commands: list[str]) -> None:
    """Exécute une suite de commandes sur la machine distante en une seule
    connexion SSH (jointes par &&), plutôt qu'une connexion par commande —
    plus rapide, et surtout nécessaire ici : si la première commande coupe
    déjà la route retour (flush de l'IP), on veut que les commandes suivantes
    s'exécutent quand même dans le MÊME shell distant, sans dépendre d'une
    nouvelle connexion qui ne pourrait plus aboutir.
    """
    remote_cmd = " && ".join(commands)
    ssh_cmd = ["ssh", "-o", "ConnectTimeout=5", f"{SSH_USER}@{host}", remote_cmd]
    print(f"$ ssh {SSH_USER}@{host} \"{remote_cmd}\"")
    try:
        result = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        # Timeout attendu si l'interface changée est celle du SSH lui-même :
        # la connexion se fige avant que le client ne reçoive la réponse,
        # mais les commandes ont probablement fini de s'exécuter côté distant.
        print("  [INFO] Pas de réponse SSH (probable coupure de la route retour "
              "— normal si l'interface changée est celle du SSH). "
              "Vérifie avec une nouvelle connexion vers la nouvelle IP.")
        return

    if result.stdout.strip():
        print(result.stdout.strip())
    if result.returncode != 0:
        print(result.stderr.strip(), file=sys.stderr)
        raise SystemExit(f"Échec SSH vers {host} (code {result.returncode})")


def change_ip(machine: dict, new_ip: str) -> None:
    interface = machine["interface"]
    cidr = machine["cidr"]
    commands = [
        f"sudo ip addr flush dev {interface}",
        f"sudo ip addr add {new_ip}/{cidr} dev {interface}",
        f"sudo ip link set {interface} up",
    ]
    print(f"Changement d'IP sur {machine['name']} ({machine['host']}) → {new_ip}")
    run_remote(machine["host"], commands)


def find_machine(name: str) -> dict:
    for m in MACHINES:
        if m["name"] == name:
            return m
    sys.exit(f"Machine inconnue : '{name}'. Utilise --list pour voir les noms disponibles.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Change l'IP d'une machine Linux du réseau via SSH.")
    # Le groupe n'est plus "required" : appeler le script sans aucun argument
    # est maintenant un cas valide, traité plus bas comme un --random implicite.
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument("--random", action="store_true", help="Tire une machine au hasard dans la liste")
    group.add_argument("--machine", help="Nom exact de la machine à cibler (voir --list)")
    group.add_argument("--list", action="store_true", help="Lister les machines connues")
    parser.add_argument("--new-ip", help="IP à assigner (sinon tirée aléatoirement dans le sous-réseau)")
    args = parser.parse_args()

    if args.list:
        for m in MACHINES:
            print(f"{m['name']}: {m['host']} (interface {m['interface']}, /{m['cidr']})")
        return

    # NOUVEAU : aucun argument fourni → comportement par défaut = --random.
    # C'est ce qui permet de tout déclencher juste avec `python change_ip_fleet.py`.
    if not args.random and not args.machine:
        args.random = True

    if args.random:
        machine = random.choice(MACHINES)
        print(f"Machine tirée au hasard : {machine['name']}")
    else:
        machine = find_machine(args.machine)

    new_ip = args.new_ip or pick_new_ip(machine["host"], machine["cidr"])
    change_ip(machine, new_ip)


if __name__ == "__main__":
    main()