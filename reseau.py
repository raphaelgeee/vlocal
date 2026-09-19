"""Vlocal 1.4.2 : IPv4 d'abord.

Sur un réseau où IPv6 est annoncé mais ne passe pas (Wi-Fi d'hôtel, d'entreprise,
certaines box), Python essaie l'adresse IPv6 en premier et attend l'expiration du
délai, pour chaque connexion, avant de tenter IPv4. curl et les navigateurs
basculent tout seuls (« happy eyeballs ») ; urllib, non. Conséquence mesurée le
19/09/2026 : le téléchargement des modèles restait bloqué des minutes sur chaque
fichier alors que le réseau fonctionnait.

On trie donc les adresses résolues : IPv4 en premier, IPv6 ensuite (toujours
essayées si IPv4 échoue). Un réseau IPv6 seul (NAT64) n'est pas touché : il n'y a
alors que des adresses IPv6. Appliqué une fois, pour tout le processus.
"""
import socket

_ORIGINAL = socket.getaddrinfo


def _ipv4_d_abord(*args, **kwargs):
    res = _ORIGINAL(*args, **kwargs)
    try:
        return sorted(res, key=lambda r: 0 if r[0] == socket.AF_INET else 1)
    except Exception:
        return res


def appliquer():
    if socket.getaddrinfo is not _ipv4_d_abord:
        socket.getaddrinfo = _ipv4_d_abord
