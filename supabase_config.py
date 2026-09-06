"""
Vlocal : coordonnées du backend Supabase.

Ces valeurs sont publiques par conception : la clé « anon » ne donne accès
qu'à ce que les politiques RLS autorisent (aucune lecture des tables), et
les edge functions appelées par l'app ne renvoient que des données publiques
(dernière version, accusé de réception d'un diagnostic).

L'app utilise le backend pour trois choses, toutes documentées dans le README :
  - connaître la dernière version publiée (get-latest-version) ;
  - envoyer un diagnostic si l'utilisateur le demande (submit-feedback) ;
  - la télémétrie minimale déclarée (tables installs et usage_days, cf. telemetry.py).

Pour un déploiement indépendant (fork), il suffit de changer ces deux valeurs
ou de définir VLOCAL_SUPABASE_URL / VLOCAL_SUPABASE_ANON_KEY dans l'environnement.
"""
import os

SUPABASE_URL = os.environ.get(
    "VLOCAL_SUPABASE_URL", "https://lvcqgfjyhjujqjgrckeg.supabase.co").rstrip("/")
SUPABASE_ANON_KEY = os.environ.get(
    "VLOCAL_SUPABASE_ANON_KEY",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imx2Y3FnZmp5aGp1anFqZ3Jja2VnIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODEzNTQ4NjEsImV4cCI6MjA5NjkzMDg2MX0."
    "L7eqPZdFKlctuyMHzsE4K69SapobRsSBgOWKC83fS24")


def edge_url(name: str) -> str:
    """URL d'une edge function (ex. get-latest-version)."""
    return f"{SUPABASE_URL}/functions/v1/{name}"


def rest_url(table: str) -> str:
    """URL PostgREST d'une table (ex. installs)."""
    return f"{SUPABASE_URL}/rest/v1/{table}"


def auth_headers() -> dict:
    return {"apikey": SUPABASE_ANON_KEY, "Authorization": "Bearer " + SUPABASE_ANON_KEY}
