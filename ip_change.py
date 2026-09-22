"""
Change la configuration IP d'une interface réseau — Windows ou Linux.

⚠️ Nécessite des privilèges administrateur (Windows) ou root/sudo (Linux).
⚠️ Modifie une vraie interface réseau : vérifie bien le nom d'interface et
   les valeurs avant de lancer, une mauvaise IP/passerelle peut te couper
   l'accès réseau à la machine.

Usage :
    python change_ip.py --interface "Ethernet" --ip 10.0.0.15 --mask 255.255.255.0 --gateway 10.0.0.254
    python change_ip.py --interface eth0 --ip 10.0.0.15 --cidr 24 --gateway 10.0.0.254
    python change_ip.py --interface "Ethernet" --dhcp        # repasser en DHCP (Windows)
"""

import argparse
import ctypes     # accès aux fonctions bas niveau de Windows (ici : IsUserAnAdmin)
import platform   # détecte l'OS courant (Windows / Linux) pour choisir la bonne méthode
import subprocess # exécute les commandes système (netsh, ip) et récupère leur sortie
import sys


def is_admin() -> bool:
    """Vérifie si le script tourne avec des privilèges élevés.

    Nécessaire car changer la config IP d'une interface est une opération
    système sensible : Windows et Linux la refusent tous les deux si on n'a
    pas les droits (administrateur / root). Mieux vaut le détecter et
    prévenir clairement plutôt que de laisser netsh/ip échouer avec un
    message d'erreur cryptique.
    """
    system = platform.system()
    if system == "Windows":
        try:
            # IsUserAnAdmin() est une fonction de l'API Windows (shell32.dll),
            # accessible via ctypes puisque Python n'a pas d'équivalent natif
            # multiplateforme pour ça.
            return ctypes.windll.shell32.IsUserAnAdmin() != 0
        except Exception:
            return False
    else:
        # Sous Linux, l'UID 0 correspond toujours à root
        import os
        return os.geteuid() == 0


def run(cmd: list[str]) -> None:
    """Exécute une commande système et affiche ce qu'elle fait, en la faisant
    échouer bruyamment (SystemExit) si le code de retour n'est pas 0.

    Centraliser l'exécution ici (plutôt que d'appeler subprocess.run direct-
    ement dans chaque fonction) évite de dupliquer la gestion d'erreur et
    l'affichage pour chaque commande netsh/ip.
    """
    print(f"$ {' '.join(cmd)}")
    # capture_output=True récupère stdout/stderr au lieu de les laisser
    # s'afficher bruts ; text=True les décode en str plutôt qu'en bytes
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.returncode != 0:
        # Un code de retour non nul signifie que netsh/ip a rejeté la commande
        # (ex: interface inexistante, IP invalide, droits insuffisants)
        print(result.stderr.strip(), file=sys.stderr)
        raise SystemExit(f"Échec de la commande (code {result.returncode})")


def set_ip_windows(interface: str, ip: str, mask: str, gateway: str | None) -> None:
    """Construit et lance la commande netsh pour passer l'interface en IP statique.

    Équivalent en ligne de commande de ce qu'on ferait manuellement dans
    Centre Réseau > Propriétés de la carte > Protocole IPv4 > IP statique.
    """
    cmd = [
        "netsh", "interface", "ip", "set", "address",
        f"name={interface}", "static", ip, mask,
    ]
    if gateway:
        # La passerelle est optionnelle : netsh accepte de configurer une IP
        # sans route par défaut (utile pour un réseau isolé sans sortie)
        cmd.append(gateway)
    run(cmd)


def set_dhcp_windows(interface: str) -> None:
    """Repasse l'interface en obtention automatique d'IP (DHCP)."""
    run(["netsh", "interface", "ip", "set", "address", f"name={interface}", "dhcp"])


def set_dns_windows(interface: str, dns: list[str]) -> None:
    """Configure un ou plusieurs serveurs DNS sur l'interface.

    netsh gère différemment le premier DNS (serveur principal, "set dns") et
    les suivants (serveurs secondaires, "add dns" avec un index de priorité)
    — d'où la distinction entre dns[0] et le reste de la liste.
    """
    run(["netsh", "interface", "ip", "set", "dns", f"name={interface}", "static", dns[0]])
    for extra_dns in dns[1:]:
        run(["netsh", "interface", "ip", "add", "dns", f"name={interface}", extra_dns, "index=2"])


def set_ip_linux(interface: str, ip: str, cidr: int, gateway: str | None) -> None:
    """Équivalent Linux de set_ip_windows, via la commande `ip` (iproute2)."""
    # flush retire toutes les IP déjà assignées à l'interface, pour éviter de
    # se retrouver avec l'ancienne ET la nouvelle IP en même temps
    run(["ip", "addr", "flush", "dev", interface])
    # Assigne la nouvelle IP en notation CIDR (ex: 10.0.0.15/24)
    run(["ip", "addr", "add", f"{ip}/{cidr}", "dev", interface])
    # S'assure que l'interface est active (elle peut être "down" après un flush)
    run(["ip", "link", "set", interface, "up"])
    if gateway:
        run(["ip", "route", "add", "default", "via", gateway, "dev", interface])


def mask_to_cidr(mask: str) -> int:
    """Convertit un masque décimal (255.255.255.0) en notation CIDR (24).

    Principe : chaque octet du masque est converti en binaire, et on compte
    le nombre total de bits à 1 (ex: 255 = 11111111 = 8 bits ; 255.255.255.0
    donne 8+8+8+0 = 24). C'est nécessaire car Linux (commande `ip`) attend du
    CIDR, alors que Windows (netsh) attend un masque décimal — deux formats
    différents pour la même information.
    """
    return sum(bin(int(octet)).count("1") for octet in mask.split("."))


def main() -> None:
    parser = argparse.ArgumentParser(description="Change la config IP d'une interface réseau.")
    parser.add_argument("--interface", required=True, help="Nom de l'interface (ex: Ethernet, eth0)")
    parser.add_argument("--ip", help="Nouvelle adresse IP (ex: 10.0.0.15)")
    parser.add_argument("--mask", help="Masque de sous-réseau, format décimal (ex: 255.255.255.0)")
    parser.add_argument("--cidr", type=int, help="Masque en notation CIDR (ex: 24)")
    parser.add_argument("--gateway", help="Passerelle par défaut (optionnel)")
    parser.add_argument("--dns", nargs="*", help="Serveur(s) DNS, Windows uniquement (optionnel)")
    parser.add_argument("--dhcp", action="store_true", help="Repasser l'interface en DHCP (Windows)")
    args = parser.parse_args()

    # On vérifie les droits AVANT de faire quoi que ce soit d'autre : pas la
    # peine de valider les arguments si de toute façon la commande va échouer
    # faute de privilèges.
    if not is_admin():
        sys.exit("Ce script doit être lancé en administrateur (Windows) ou avec sudo (Linux).")

    system = platform.system()

    if system == "Windows":
        if args.dhcp:
            # Mode DHCP : on ignore --ip/--mask/--gateway s'ils sont fournis,
            # ils n'ont pas de sens ensemble avec --dhcp
            set_dhcp_windows(args.interface)
        else:
            # Mode IP statique : --ip et --mask sont obligatoires sous
            # Windows (netsh ne sait pas déduire l'un à partir de l'autre)
            if not args.ip or not args.mask:
                sys.exit("--ip et --mask sont requis sous Windows pour une IP statique.")
            set_ip_windows(args.interface, args.ip, args.mask, args.gateway)
            if args.dns:
                set_dns_windows(args.interface, args.dns)

    elif system == "Linux":
        if not args.ip:
            sys.exit("--ip est requis sous Linux.")
        # Sous Linux on accepte --cidr OU --mask (contrairement à Windows qui
        # veut toujours un masque décimal) : si --cidr n'est pas donné, on le
        # calcule à partir de --mask via mask_to_cidr()
        cidr = args.cidr if args.cidr is not None else (mask_to_cidr(args.mask) if args.mask else None)
        if cidr is None:
            sys.exit("Fournis --cidr (ex: 24) ou --mask (ex: 255.255.255.0) sous Linux.")
        set_ip_linux(args.interface, args.ip, cidr, args.gateway)

    else:
        # macOS ou autre : volontairement non géré, pour ne pas donner une
        # fausse impression de compatibilité (la syntaxe des commandes réseau
        # y est différente — ifconfig/networksetup)
        sys.exit(f"OS non géré : {system}")

    print("Configuration IP appliquée avec succès.")


if __name__ == "__main__":
    # Ce bloc ne s'exécute que si le fichier est lancé directement
    # (python change_ip.py ...), pas s'il est importé comme module ailleurs.
    main()