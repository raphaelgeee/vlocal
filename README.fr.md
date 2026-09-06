# Vlocal

Dictée vocale et transcription de réunions pour macOS, entièrement sur votre Mac.

Vous maintenez un raccourci, vous parlez, vous relâchez : le texte s'écrit là où
est votre curseur, dans n'importe quelle application. Vous enregistrez une
réunion et vous obtenez la transcription avec qui a dit quoi. Rien n'est envoyé :
les modèles tournent sur le GPU Apple Silicon (ou le CPU), et l'audio, les
transcriptions et les empreintes vocales ne quittent jamais la machine.

Vlocal est gratuit et open source (AGPL-3.0). L'app a été payante de juin à
août 2026 ; depuis la version 1.1.0 (septembre 2026), il n'y a plus de licence.

English: [README.md](README.md).

## Ce que fait l'app

- **Dictée au curseur.** Raccourci par défaut : maintenir `Ctrl + Cmd`, parler,
  relâcher. Fonctionne dans toutes les apps ; sans champ de texte actif, le texte
  est copié dans le presse-papier. Les dictées longues sont transcrites pendant
  que vous parlez : l'attente au relâchement reste d'environ une seconde.
- **Réunions.** Enregistre le micro (et, au choix, l'audio système d'une visio),
  transcrit au fil de la réunion, puis sépare les locuteurs par empreintes vocales
  locales. Un locuteur nommé est reconnu dans les réunions suivantes.
- **Rappels** compris en français naturel (« rappelle-moi demain à 9h de... »),
  délivrés en notification macOS.
- **Glossaire** de noms propres et de termes métier, appliqué à chaque transcription.
- **Connecteur Obsidian** : les dictées peuvent être rangées dans un coffre existant,
  ou Vlocal crée le coffre « Vlocal, ma voix » où dictées, réunions (par locuteur)
  et rappels sont conservés en Markdown, avec un CLAUDE.md qui décrit la
  structure à un assistant qui travaille sur vos fichiers.
- Interface en français et en anglais.

## Prérequis

- Mac Apple Silicon (M1 ou plus récent). Les Mac Intel ne sont pas pris en charge.
- macOS 11 ou plus récent. Le moteur GPU demande macOS 14 ou plus ; en dessous,
  le moteur CPU prend le relais automatiquement (même modèle, plus lent).
- Environ 3 Go de disque pour les modèles, téléchargés au premier lancement.

## Installation

Téléchargez le DMG signé et notarisé sur [vlocal.org](https://www.vlocal.org),
ouvrez-le, glissez Vlocal dans Applications. Au premier lancement, l'app
télécharge les modèles, demande les autorisations Micro et Accessibilité, et vous
demande votre prénom et votre nom (voir « Données » ci-dessous).

## Données : ce qui sort de votre Mac, et ce qui n'en sort jamais

Jamais : l'audio, le texte dicté, le contenu des réunions, les transcriptions,
les empreintes vocales, les noms de fichiers, l'adresse email, le nom de la
machine. Tout cela reste sur le disque, dans
`~/Library/Application Support/Vlocal`.

Avec votre accord (demandé une fois à l'installation, modifiable dans Réglages >
Données partagées), l'app envoie une fois par jour :

| Champ | Usage |
| --- | --- |
| un identifiant d'installation aléatoire (UUID) | compter les installations, sans lien avec la machine |
| prénom et nom, tels que saisis | savoir qui utilise Vlocal |
| version de Vlocal et de macOS | support |
| par jour : nombre de dictées, nombre de mots, temps gagné estimé | mesurer l'usage réel |

Rien d'autre. Le contenu exact de l'envoi est construit dans
[telemetry.py](telemetry.py) (`build_rows`) et verrouillé par
[tests/test_telemetry.py](tests/test_telemetry.py), qui échoue si un champ est
ajouté. Le temps gagné suit la formule du tableau de bord : mots à 40 mots par
minute au clavier contre 150 à la voix.

Deux autres appels réseau existent : la vérification de version
(`get-latest-version`, aucune donnée personnelle) et le bouton « Envoyer un
diagnostic » des Réglages, qui transmet le message que vous rédigez et, si vous
le renseignez, votre email.

## Comment ça marche

| Composant | Implémentation |
| --- | --- |
| Parole vers texte | Whisper large-v3-turbo. GPU : [mlx-whisper](https://github.com/ml-explore/mlx-examples), poids 8 bits. CPU : [faster-whisper](https://github.com/SYSTRAN/faster-whisper) int8, plus un modèle small pour les machines à peu de mémoire. |
| Détection de parole | VAD Silero (via faster-whisper) et un seuil RMS partagé avec le live-tail. |
| Séparation des locuteurs | [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx), empreintes CAM++ 192-d, k-means sphérique, nombre de voix par écart spectral (silhouette en repli) plafonné par la durée, frontières recalées sur la parole. |
| Mise en forme | Règles déterministes ([processor.py](processor.py)) ; aucun modèle génératif, rien n'est réécrit. |
| Stockage | SQLite en mode WAL ([storage.py](storage.py)). |
| Interface | Un seul fichier HTML ([vlocal-interface.html](vlocal-interface.html)) dans une fenêtre pywebview, un overlay de dictée flottant, une icône de barre de menus. |
| Fiabilité | Le micro est capturé dans un sous-process jetable, tué et relancé s'il se fige ; l'inférence GPU a un délai proportionnel à l'audio et un repli CPU temporaire ; un superviseur borne chaque étape d'une dictée. |

Détails dans [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Lancer depuis les sources

```bash
git clone https://github.com/raphaelgeee/vlocal.git
cd vlocal
python3.12 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python download_whisper.py     # modèles CPU (faster-whisper)
./venv/bin/python download_mlx.py         # modèle GPU (MLX), Apple Silicon
./venv/bin/python app.py
```

Le wheel `mlx-metal` doit cibler macOS 14, sinon le GPU échoue chez tout
utilisateur dont le macOS est plus ancien que la machine de build.
`build_app.sh` le vérifie (étape 3ter) avant de produire un DMG.

L'insertion au curseur passe par l'API Accessibilité. macOS accorde cette
autorisation à un binaire précis : depuis les sources, accordez-la à votre
interpréteur Python, et de nouveau après chaque rebuild de l'app.

## Tests

```bash
./venv/bin/python -m unittest discover -s tests -p "test_livetail.py"
./venv/bin/python -m unittest discover -s tests -p "test_gpu_resilience.py"
./venv/bin/python -m unittest discover -s tests -p "test_telemetry.py"
./venv/bin/python -m unittest discover -s tests -p "test_speaker_count.py"
./venv/bin/python tests/test_processor.py
./venv/bin/python tests/test_reminders.py
./venv/bin/python tests/test_storage.py
./venv/bin/python tests/test_vocal_commands.py
./venv/bin/python tests/test_diarization_v18.py
```

Le banc de qualité de la séparation des locuteurs (`tests/rd`) s'appuie sur des
enregistrements réels de réunions et n'est pas publié.

## Construire l'app

`./build_app.sh` produit `dist/Vlocal.app` et `dist/Vlocal-<VERSION>.dmg`
(PyInstaller, signature ad hoc). `notarize.sh` signe avec un certificat
Developer ID et notarise chez Apple. La publication vers le canal de mise à jour
est décrite dans [docs/RELEASING.md](docs/RELEASING.md) ; elle demande l'accès au
bucket Cloudflare R2 et au projet Supabase du mainteneur : lui seul peut livrer
une mise à jour aux installations existantes. Un fork peut pointer
[supabase_config.py](supabase_config.py) vers son propre backend
([docs/SUPABASE.md](docs/SUPABASE.md)).

## Organisation du dépôt

Voir la section « Project layout » du [README.md](README.md).

## Contribuer, sécurité

[CONTRIBUTING.md](CONTRIBUTING.md) et [SECURITY.md](SECURITY.md).

## Licence

AGPL-3.0, voir [LICENSE](LICENSE). Une licence commerciale est proposée aux
entreprises qui souhaitent intégrer Vlocal sans les obligations de l'AGPL :
[COMMERCIAL-LICENSE.md](COMMERCIAL-LICENSE.md).
