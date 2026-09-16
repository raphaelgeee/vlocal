#!/usr/bin/env python3
"""Banc du modèle de langage local (LFM2.5-1.2B, MLX) sur de VRAIES dictées.

    ./venv/bin/python tools/llm_bench.py <dossier_modele> <dictations.json> [meetings.db] [--n 40] [--out rapport.md]

Mesure, pour chaque usage envisagé (nettoyage, mail, note, prompt, liste,
anglais, rappel en JSON, compte rendu de réunion) : latence, débit, mémoire,
et pour le nettoyage la part du texte modifiée (un nettoyage qui réécrit est
un échec). Écrit un rapport Markdown avec des exemples à relire. Ne touche
pas à l'app : c'est le juge de paix avant toute intégration.
"""
import json, os, re, sys, time, difflib, sqlite3, resource

import mlx.core as mx
from mlx_lm import load, generate
from mlx_lm.sample_utils import make_sampler

REGLE = ("Tu es le moteur de mise en forme de Vlocal, une app de dictée. Tu reçois le texte dicté par la personne. "
         "Tu ne changes jamais le sens, tu n'ajoutes aucune information, tu n'inventes rien, tu gardes la langue du texte "
         "sauf consigne contraire. Tu réponds uniquement par le texte demandé, sans commentaire ni guillemets.")

TACHES = {
    "propre": ("Nettoie cette dictée sans la reformuler : retire les hésitations (euh, hum), les répétitions involontaires "
               "et les faux départs, rétablis la ponctuation et les majuscules, écris les nombres, dates et heures en chiffres "
               "(« quatorze heures » devient « 14 h »). Garde chaque mot porteur de sens, dans l'ordre. Un seul paragraphe si la dictée n'en a qu'un."),
    "mail": ("Mets cette dictée en forme de courriel prêt à envoyer : une formule d'ouverture adaptée, des paragraphes courts, "
             "une formule de politesse, puis la signature « Raphael ». Ne change pas le fond."),
    "note": ("Mets cette dictée en forme de note : un titre court sur la première ligne, puis des puces courtes qui reprennent "
             "chaque idée, sans rien perdre ni ajouter."),
    "prompt": ("Transforme cette dictée en un prompt clair pour un assistant IA, avec les sections Contexte, Tâche, Contraintes "
               "et Format attendu. Tout ce qui n'est pas dit reste absent : n'invente aucune contrainte."),
    "liste": ("Transforme cette dictée en liste à puces, une action ou une idée par puce, verbes à l'infinitif quand ce sont des actions."),
    "en": ("Traduis cette dictée en anglais naturel, registre professionnel, sans rien ajouter ni retirer."),
}

RAPPELS = [
    "rappelle-moi d'appeler Paul demain à dix heures",
    "n'oublie pas le devis pour Marie jeudi matin",
    "pense à envoyer le contrat avant dix-sept heures trente",
    "remind me to call the accountant on Monday at 9",
    "rappelle-moi dans deux heures de sortir le gâteau du four",
    "tu peux me rappeler vendredi prochain à quatorze heures le point avec Gaspard",
    "je vais faire les courses ce soir",   # pas un rappel : doit répondre null
]
RAPPEL_REGLE = ("Tu extrais un rappel d'une phrase dictée. Réponds UNIQUEMENT par un objet JSON : "
                '{"rappel": true|false, "titre": "…", "quand": "expression de temps telle que dite", "iso": "AAAA-MM-JJTHH:MM ou null", "avant_min": 0}. '
                "La date de référence est {now} (heure locale, Europe/Paris). Si la phrase n'est pas une demande de rappel, réponds {\"rappel\": false}.")

CR_REGLE = ("Tu rédiges le compte rendu d'une réunion à partir de sa transcription, où chaque prise de parole est précédée du nom "
            "ou du numéro du locuteur. Réponds en Markdown avec exactement ces sections : « Décisions », « Actions » (une puce par "
            "action : qui, quoi, pour quand si c'est dit), « Sujets ouverts », « Noms entendus » (les prénoms que les participants "
            "se donnent ou s'adressent, avec le locuteur probable). Rien qui ne soit dans la transcription.")


def rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)


def chat(tok, system, user):
    return tok.apply_chat_template([{"role": "system", "content": system}, {"role": "user", "content": user}],
                                   tokenize=False, add_generation_prompt=True)


def run(model, tok, system, user, max_tokens=600):
    prompt = chat(tok, system, user)
    n_in = len(tok.encode(prompt))
    t = time.time()
    out = generate(model, tok, prompt=prompt, max_tokens=max_tokens, sampler=make_sampler(temp=0.0), verbose=False)
    dt = time.time() - t
    n_out = len(tok.encode(out))
    return out.strip(), dt, n_in, n_out


def similarite(a, b):
    """Part des mots du texte d'origine retrouvés, dans l'ordre, dans le résultat."""
    wa = re.findall(r"\w+", a.lower()); wb = re.findall(r"\w+", b.lower())
    return difflib.SequenceMatcher(None, wa, wb).ratio()


def main():
    args = sys.argv[1:]
    n = 40; out_path = None
    if "--n" in args: n = int(args[args.index("--n") + 1]); del args[args.index("--n"):args.index("--n") + 2]
    if "--out" in args: out_path = args[args.index("--out") + 1]; del args[args.index("--out"):args.index("--out") + 2]
    model_dir, dict_path = args[0], args[1]
    db_path = args[2] if len(args) > 2 else None
    out_path = out_path or os.path.join(os.path.dirname(dict_path), "bench-%s.md" % os.path.basename(model_dir))

    t0 = time.time()
    model, tok = load(model_dir)
    t_load = time.time() - t0
    dictees = [d["content"].strip() for d in json.load(open(dict_path))][:n]
    L = ["# Banc LFM2.5 · %s" % os.path.basename(model_dir), "",
         "Chargement : %.1f s · RSS après chargement : %.0f Mo · pic Metal : %.0f Mo" % (t_load, rss_mb(), mx.get_peak_memory() / 1e6), ""]

    # 1) nettoyage sur toutes les dictées : latence + part modifiée
    stats = []
    exemples = []
    for i, d in enumerate(dictees):
        out, dt, ni, no = run(model, tok, REGLE, TACHES["propre"] + "\n\nDictée :\n" + d, max_tokens=max(64, int(len(tok.encode(d)) * 1.5)))
        sim = similarite(d, out)
        stats.append((len(d.split()), dt, ni, no, sim))
        if i < 8 or sim < 0.7:
            exemples.append((d, out, sim, dt))
    mots = sum(s[0] for s in stats); lat = [s[1] for s in stats]; sims = [s[4] for s in stats]
    tps = sum(s[3] for s in stats) / max(1e-9, sum(lat))
    L += ["## Nettoyage (« propre ») sur %d dictées, %d mots" % (len(stats), mots), "",
          "| mesure | valeur |", "|---|---|",
          "| latence médiane | %.2f s |" % sorted(lat)[len(lat) // 2],
          "| latence max | %.2f s |" % max(lat),
          "| latence pour 60 mots (interpolée) | %.2f s |" % (sorted(lat)[len(lat) // 2] * 60 / max(1, sorted(s[0] for s in stats)[len(stats) // 2])),
          "| débit de génération | %.0f tok/s |" % tps,
          "| similarité médiane texte/résultat | %.2f |" % sorted(sims)[len(sims) // 2],
          "| dictées modifiées à plus de 30 %% | %d |" % sum(1 for s in sims if s < 0.7),
          "| pic mémoire Metal | %.0f Mo |" % (mx.get_peak_memory() / 1e6), ""]
    L += ["### Exemples de nettoyage", ""]
    for d, o, sim, dt in exemples[:12]:
        L += ["**Dictée** : " + d.replace("\n", " ")[:600], "", "**Résultat** (%.2f s, similarité %.2f) : " % (dt, sim) + o.replace("\n", " ")[:700], "", "---", ""]

    # 2) formats sur 6 dictées représentatives (les plus courtes ≥ 25 mots, variées)
    cibles = [d for d in dictees if 25 <= len(d.split()) <= 120][:6]
    for tache in ("mail", "note", "prompt", "liste", "en"):
        L += ["## Format « %s »" % tache, ""]
        for d in cibles[:3]:
            out, dt, ni, no = run(model, tok, REGLE, TACHES[tache] + "\n\nDictée :\n" + d, max_tokens=500)
            L += ["**Dictée** : " + d.replace("\n", " ")[:400], "", "**Résultat** (%.2f s, %d tok) :" % (dt, no), "", "```", out[:1200], "```", ""]

    # 3) rappels en JSON
    L += ["## Rappels (sortie JSON)", "", "| phrase | réponse | latence |", "|---|---|---|"]
    now = time.strftime("%Y-%m-%dT%H:%M, %A")
    for ph in RAPPELS:
        out, dt, ni, no = run(model, tok, RAPPEL_REGLE.replace("{now}", now), ph, max_tokens=120)
        ok = "✓" if out.startswith("{") and out.rstrip().endswith("}") else "✗ pas du JSON"
        L.append("| %s | `%s` %s | %.2f s |" % (ph, out.replace("\n", " ")[:160], ok, dt))
    L.append("")

    # 4) compte rendu de réunion (la plus longue disponible)
    if db_path and os.path.exists(db_path):
        con = sqlite3.connect(db_path)
        row = con.execute("select titre, speaker_blocks_json, transcription_brute from meetings where length(coalesce(speaker_blocks_json,''))>1000 order by length(speaker_blocks_json) desc limit 1").fetchone()
        if row:
            titre, sbj, brut = row
            try:
                parsed = json.loads(sbj); blocks = parsed.get("blocks", parsed if isinstance(parsed, list) else []); names = parsed.get("names", {}) if isinstance(parsed, dict) else {}
            except Exception:
                blocks, names = [], {}
            lignes = []
            for b in blocks:
                spk = b.get("speaker") or b.get("name") or "?"
                lignes.append("%s : %s" % (names.get(spk, spk), (b.get("text") or "").strip()))
            transcript = "\n".join(lignes)
            n_tok = len(tok.encode(transcript))
            # on borne à ~20k tokens pour rester dans la fenêtre avec la réponse
            if n_tok > 20000:
                coupe = int(len(transcript) * 20000 / n_tok); transcript = transcript[:coupe]; n_tok = len(tok.encode(transcript))
            out, dt, ni, no = run(model, tok, CR_REGLE, "Transcription :\n" + transcript, max_tokens=900)
            L += ["## Compte rendu : « %s »" % titre, "", "%d prises de parole · %d tokens en entrée · %.1f s au total (%.0f tok/s de lecture) · pic Metal %.0f Mo" %
                  (len(blocks), ni, dt, ni / max(dt, 1e-9), mx.get_peak_memory() / 1e6), "", "```", out[:4000], "```", ""]
    L += ["", "RSS final : %.0f Mo" % rss_mb()]
    open(out_path, "w", encoding="utf-8").write("\n".join(L))
    print("rapport :", out_path)


if __name__ == "__main__":
    main()
