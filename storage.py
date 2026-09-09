#!/usr/bin/env python3
"""
Vlocal — Persistance locale (SQLite, WAL) : rappels, dictées, réunions, glossaire.

- DB : ~/Library/Application Support/Vlocal/vlocal.db (créée si absente)
- Tables : tasks (rappels), dictations, meetings, glossary, snippets.
- Thread-safe : un seul connect partagé, lock interne, check_same_thread=False.
- Migrations simples (PRAGMA user_version).
- Chiffrement au repos : non implémenté — la DB reste en clair dans
  ~/Library/Application Support/Vlocal.

Pas de réseau. Pas de PII vers l'extérieur.
"""

import os
import sqlite3
import threading
import time
from pathlib import Path

APP_DIR = Path(os.path.expanduser("~/Library/Application Support/Vlocal"))
DB_PATH = APP_DIR / "vlocal.db"
SCHEMA_VERSION = 9  # v1.2.0 : compteurs d'usage complets (réunions) + reprise de l'historique

# Temps gagné par mot dicté, en secondes : frappe à 40 mots/min contre voix à
# 150 mots/min, soit 1,5 s - 0,4 s. Même formule que le tableau de bord (accueil).
SECONDS_SAVED_PER_WORD = 60.0 / 40.0 - 60.0 / 150.0


class Store:
    """Couche SQLite minimale. Sûr à utiliser depuis plusieurs threads."""

    def __init__(self, path=None):
        self.path = Path(path) if path else DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        # Gardes mémoire : épinglent le comportement quel que soit le build
        # SQLite de l'interpréteur. Mesuré ici : libsqlite3 d'Apple (python3
        # système) -> cache_size défaut 2000 pages x 4096 = 8 Mio ; CPython du
        # venv et bundle PyInstaller (SQLite 3.53) -> déjà -2000 (2 Mio).
        # cache_size=-2000 borne le page cache à 2 Mio (gain RSS ~-6 Mo
        # uniquement sur le chemin python système) ; mmap_size=0 est un no-op
        # aujourd'hui (défaut mesuré = 0), posé en garde explicite.
        self._conn.execute("PRAGMA cache_size=-2000")
        self._conn.execute("PRAGMA mmap_size=0")
        self._conn.execute("PRAGMA foreign_keys=ON")
        # Audit v18 SEC-02 (RGPD, droit à l'effacement) : secure_delete zéro-remplit
        # les pages libérées (pas de fragment de transcription/dictée lisible après
        # un DELETE), et on borne le WAL pour qu'il ne conserve pas longtemps des
        # données en clair. Le TRUNCATE explicite est déclenché par _checkpoint()
        # après chaque suppression (voir les méthodes delete_*/clear_*).
        self._conn.execute("PRAGMA secure_delete=ON")
        self._conn.execute("PRAGMA wal_autocheckpoint=400")
        self._migrate()

    def _checkpoint(self):
        """Vide le WAL dans la base ET TRONQUE le fichier -wal : les données
        supprimées par l'utilisateur ne survivent pas en clair dans un WAL
        résiduel (effacement RGPD réellement effectif). Best-effort, jamais
        d'exception (le checkpoint peut échouer si une autre lecture est en cours
        — il sera retenté au prochain appel)."""
        try:
            with self._lock:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass

    def _migrate(self):
        with self._lock:
            v = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if v < 1:
                self._conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS tasks(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        titre TEXT NOT NULL,
                        echeance TEXT,
                        echeance_iso TEXT,
                        details TEXT,
                        statut TEXT NOT NULL DEFAULT 'open',
                        created_at REAL NOT NULL,
                        done_at REAL,
                        notif_scheduled INTEGER NOT NULL DEFAULT 0,
                        source_raw_text TEXT
                    );
                    """
                )
            if v < 2:
                self._conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS meetings(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        titre TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        duree_audio_s REAL NOT NULL DEFAULT 0,
                        duree_transcription_s REAL NOT NULL DEFAULT 0,
                        wav_path TEXT,
                        transcription_brute TEXT,
                        transcription_structuree TEXT,
                        status TEXT NOT NULL DEFAULT 'recording'
                    );
                    """
                )
            if v < 3:
                # v13 — Glossaire personnel (corrections déterministes noms
                # propres + acronymes). Appliqué sur la sortie Whisper
                # (processor.apply_glossary) et en biais initial_prompt
                # (engine.py).
                # UNIQUE(incorrect collate nocase) : pas de doublon insensible
                # à la casse, on remplace au lieu de dupliquer.
                self._conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS glossary(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        incorrect TEXT NOT NULL,
                        correct TEXT NOT NULL,
                        notes TEXT DEFAULT '',
                        created_at REAL NOT NULL DEFAULT 0,
                        UNIQUE(incorrect COLLATE NOCASE)
                    );
                    CREATE INDEX IF NOT EXISTS idx_glossary_incorrect
                        ON glossary(incorrect COLLATE NOCASE);
                    """
                )
            if v < 4:
                # v15 — confidence par mot (Whisper large-v3-turbo) sur meetings.
                # ALTER idempotent : on ignore l'erreur si la colonne existe.
                for ddl in (
                    "ALTER TABLE meetings ADD COLUMN confidence_json TEXT",
                    "ALTER TABLE meetings ADD COLUMN avg_confidence REAL",
                ):
                    try:
                        self._conn.execute(ddl)
                    except Exception:
                        pass
            if v < 5:
                # v2 — Raccourcis vocaux : on dicte un déclencheur (« ma
                # signature ») -> insertion d'un bloc de texte enregistré.
                self._conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS snippets(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        trigger TEXT NOT NULL,
                        expansion TEXT NOT NULL,
                        created_at REAL NOT NULL DEFAULT 0,
                        UNIQUE(trigger COLLATE NOCASE)
                    );
                    """
                )
            if v < 6:
                # v18 — Diarisation. speaker_blocks_json stocke la liste des
                # blocs {speaker,start,end,text} ET la map de renommage
                # {"SPEAKER_00":"Marie"} (clé "names") pour la réunion.
                # confidence_json reste pour la confiance moyenne globale.
                try:
                    self._conn.execute(
                        "ALTER TABLE meetings ADD COLUMN speaker_blocks_json TEXT")
                except Exception:
                    pass
            if v < 7:
                # v18.4 — Historique des DICTÉES (léger : texte seul, pas d'audio).
                # Persisté uniquement si l'utilisateur l'autorise (réglage de
                # stockage). Sert l'onglet Historique.
                self._conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS dictations(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        content TEXT NOT NULL,
                        created_at REAL NOT NULL DEFAULT 0,
                        mode TEXT,
                        chars INTEGER NOT NULL DEFAULT 0
                    );
                    """
                )
            if v < 8:
                # v1.1.0 — Compteurs d'usage par JOUR (aucun contenu) : nombre de
                # dictées, mots, temps gagné. Alimentés à chaque dictée, que
                # l'historique soit conservé ou non. Servent le tableau de bord et
                # la télémétrie déclarée (telemetry.py).
                self._conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS usage_days(
                        day TEXT PRIMARY KEY,
                        dictations INTEGER NOT NULL DEFAULT 0,
                        words INTEGER NOT NULL DEFAULT 0,
                        seconds_saved REAL NOT NULL DEFAULT 0
                    );
                    """
                )
            if v < 9:
                # v1.2.0 — les RÉUNIONS comptent aussi (mots transcrits, durée
                # d'audio), et l'historique déjà en base est repris : le tableau
                # de bord n'est plus calculé sur les 400 dernières entrées
                # AFFICHÉES (d'où des totaux très sous-évalués), mais sur ces
                # compteurs, exacts et indépendants du réglage d'historique.
                for _sql in ("ALTER TABLE usage_days ADD COLUMN meetings INTEGER NOT NULL DEFAULT 0",
                             "ALTER TABLE usage_days ADD COLUMN meeting_words INTEGER NOT NULL DEFAULT 0",
                             "ALTER TABLE usage_days ADD COLUMN meeting_seconds REAL NOT NULL DEFAULT 0"):
                    try:
                        self._conn.execute(_sql)
                    except Exception:
                        pass          # colonne déjà présente
                self._backfill_usage_locked()
            self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    # ---------------- meetings (v12) ---------------- #
    def add_meeting(self, titre, wav_path, status="recording"):
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO meetings(titre,created_at,wav_path,status) "
                "VALUES(?,?,?,?)",
                (titre, time.time(), wav_path, status),
            )
            return cur.lastrowid

    # Liste blanche des colonnes modifiables : les NOMS de colonnes sont
    # interpolés dans le SQL d'update_meeting (les valeurs restent bindées),
    # on refuse donc toute clé inconnue. IMPORTANT : toute colonne ajoutée par
    # migration (ALTER TABLE meetings dans _migrate, paliers v<4 et v<6) doit
    # aussi être ajoutée ici, sinon un update légitime lèvera ValueError.
    _MEETING_COLS = frozenset({
        "titre", "duree_audio_s", "duree_transcription_s", "wav_path",
        "transcription_brute", "transcription_structuree", "status",
        "confidence_json", "avg_confidence", "speaker_blocks_json",
    })

    def update_meeting(self, meeting_id, **fields):
        if not fields:
            return
        for k in fields:
            if k not in self._MEETING_COLS:
                raise ValueError(f"colonne inconnue: {k}")
        cols = ", ".join(f"{k}=?" for k in fields.keys())
        vals = list(fields.values()) + [meeting_id]
        with self._lock:
            self._conn.execute(
                f"UPDATE meetings SET {cols} WHERE id=?", tuple(vals)
            )

    def list_meetings(self, limit=10):
        with self._lock:
            return [
                dict(r)
                for r in self._conn.execute(
                    "SELECT * FROM meetings ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                )
            ]

    def get_meeting(self, meeting_id):
        """v20 — Accès CIBLÉ à UNE réunion (SELECT ... WHERE id=?). Avant, tous
        les chemins de détail passaient par list_meetings(50) + filtre Python :
        50 réunions matérialisées (gros JSON inclus) pour en garder une, et les
        réunions au-delà des 50 dernières étaient INTROUVABLES (Historique)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM meetings WHERE id=?", (meeting_id,)
            ).fetchone()
        return dict(row) if row else None

    def count_meetings(self, cap=9999):
        """v20 — Comptage léger (plafonné comme l'ancien len(list(LIMIT 9999))) :
        aucune colonne lourde matérialisée."""
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM (SELECT 1 FROM meetings LIMIT ?)", (cap,)
            ).fetchone()[0]

    def count_dictations(self, cap=9999):
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM (SELECT 1 FROM dictations LIMIT ?)", (cap,)
            ).fetchone()[0]

    def list_meeting_wavs(self, cap=9999):
        """v20 — (id, wav_path) seulement : pour sommer les tailles audio sans
        charger les transcriptions/JSON par mot de toutes les réunions."""
        with self._lock:
            return [
                (r[0], r[1])
                for r in self._conn.execute(
                    "SELECT id, wav_path FROM meetings "
                    "ORDER BY created_at DESC LIMIT ?", (cap,))
            ]

    def delete_meeting(self, meeting_id, delete_wav=True):
        with self._lock:
            row = self._conn.execute(
                "SELECT wav_path FROM meetings WHERE id=?", (meeting_id,)
            ).fetchone()
            self._conn.execute("DELETE FROM meetings WHERE id=?", (meeting_id,))
        if delete_wav and row and row["wav_path"]:
            try:
                if os.path.exists(row["wav_path"]):
                    os.unlink(row["wav_path"])
            except Exception:
                pass
        self._checkpoint()   # SEC-02 : ne pas laisser la réunion supprimée en clair dans le WAL

    def clear_meetings(self, delete_wav=True):
        """v18.4 — Purge toutes les réunions (et leurs WAV). Renvoie le nombre."""
        with self._lock:
            rows = [dict(r) for r in self._conn.execute(
                "SELECT id, wav_path FROM meetings")]
            self._conn.execute("DELETE FROM meetings")
        if delete_wav:
            for r in rows:
                try:
                    if r.get("wav_path") and os.path.exists(r["wav_path"]):
                        os.unlink(r["wav_path"])
                except Exception:
                    pass
        self._checkpoint()   # SEC-02
        return len(rows)

    # ---------------- dictations (v18.4 — historique) ---------------- #
    def add_dictation(self, content, mode=None):
        content = (content or "").strip()
        if not content:
            return None
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO dictations(content,created_at,mode,chars) "
                "VALUES(?,?,?,?)",
                (content, time.time(), mode, len(content)),
            )
            return cur.lastrowid

    # ---------------- usage (v1.1.0) ---------------- #
    @staticmethod
    def word_count(text) -> int:
        return len((text or "").split())

    def _backfill_usage_locked(self) -> None:
        """v1.2.0 — Reprend l'historique DÉJÀ en base (dictées et réunions) dans
        usage_days, pour les jours ANTÉRIEURS au premier jour déjà compté (les
        jours suivants ont été alimentés en direct : on ne recompte jamais).
        Appelé une seule fois, sous le verrou de migration. Ne lève jamais."""
        try:
            row = self._conn.execute("SELECT MIN(day) FROM usage_days").fetchone()
            first = row[0] if row else None
            agg = {}
            def _add(day, key, value):
                if first and day >= first:
                    return                      # déjà compté en direct
                agg.setdefault(day, {"dictations": 0, "words": 0,
                                     "meetings": 0, "meeting_words": 0,
                                     "meeting_seconds": 0.0})[key] += value
            for content, created in self._conn.execute(
                    "SELECT content, created_at FROM dictations"):
                day = time.strftime("%Y-%m-%d", time.localtime(created or 0))
                _add(day, "dictations", 1)
                _add(day, "words", len((content or "").split()))
            for txt, brut, created, dur in self._conn.execute(
                    "SELECT transcription_structuree, transcription_brute, "
                    "created_at, duree_audio_s FROM meetings"):
                day = time.strftime("%Y-%m-%d", time.localtime(created or 0))
                _add(day, "meetings", 1)
                _add(day, "meeting_words", len(((txt or brut) or "").split()))
                _add(day, "meeting_seconds", float(dur or 0))
            for day, v in agg.items():
                self._conn.execute(
                    "INSERT INTO usage_days(day,dictations,words,seconds_saved,"
                    "meetings,meeting_words,meeting_seconds) VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(day) DO UPDATE SET "
                    "dictations=dictations+excluded.dictations, "
                    "words=words+excluded.words, "
                    "seconds_saved=seconds_saved+excluded.seconds_saved, "
                    "meetings=meetings+excluded.meetings, "
                    "meeting_words=meeting_words+excluded.meeting_words, "
                    "meeting_seconds=meeting_seconds+excluded.meeting_seconds",
                    (day, v["dictations"], v["words"],
                     v["words"] * SECONDS_SAVED_PER_WORD,
                     v["meetings"], v["meeting_words"], v["meeting_seconds"]))
            if agg:
                print(f"[usage] historique repris : {len(agg)} jour(s).")
        except Exception as e:
            print(f"[usage] reprise de l'historique ignorée ({e}).")

    def record_meeting(self, words: int, audio_s: float = 0.0, day: str = None) -> None:
        """Ajoute une réunion transcrite au compteur du jour (aucun contenu)."""
        words = max(0, int(words or 0))
        day = day or time.strftime("%Y-%m-%d")
        with self._lock:
            self._conn.execute(
                "INSERT INTO usage_days(day,meetings,meeting_words,meeting_seconds) "
                "VALUES(?,1,?,?) "
                "ON CONFLICT(day) DO UPDATE SET "
                "meetings=meetings+1, meeting_words=meeting_words+excluded.meeting_words, "
                "meeting_seconds=meeting_seconds+excluded.meeting_seconds",
                (day, words, max(0.0, float(audio_s or 0))),
            )

    def usage_stats(self, days_back: int = 30) -> dict:
        """v1.2.0 — Chiffres EXACTS du tableau de bord, lus dans usage_days :
        totaux depuis le début du comptage, mois en cours, et série journalière.
        Indépendant de l'historique conservé (qui peut être désactivé ou effacé)."""
        month = time.strftime("%Y-%m-01")
        since_day = time.strftime("%Y-%m-%d",
                                  time.localtime(time.time() - days_back * 86400))
        cols = ("dictations", "words", "seconds_saved", "meetings",
                "meeting_words", "meeting_seconds")
        sel = ", ".join(f"COALESCE(SUM({c}),0)" for c in cols)
        with self._lock:
            first = (self._conn.execute("SELECT MIN(day) FROM usage_days").fetchone() or [None])[0]
            tot = self._conn.execute(f"SELECT {sel} FROM usage_days").fetchone()
            mon = self._conn.execute(f"SELECT {sel} FROM usage_days WHERE day>=?",
                                     (month,)).fetchone()
            series = [dict(r) for r in self._conn.execute(
                "SELECT day,dictations,words,seconds_saved,meetings,meeting_words "
                "FROM usage_days WHERE day>=? ORDER BY day", (since_day,))]
        def pack(r):
            d = {c: (float(v) if "seconds" in c else int(v)) for c, v in zip(cols, r)}
            d["total_words"] = d["words"] + d["meeting_words"]
            return d
        return {"since_day": first, "total": pack(tot), "month": pack(mon),
                "series": series}

    def record_usage(self, words: int, day: str = None) -> None:
        """Ajoute une dictée de `words` mots au compteur du jour (YYYY-MM-DD,
        heure locale). Idempotence non requise : un appel = une dictée."""
        words = max(0, int(words or 0))
        day = day or time.strftime("%Y-%m-%d")
        with self._lock:
            self._conn.execute(
                "INSERT INTO usage_days(day,dictations,words,seconds_saved) "
                "VALUES(?,1,?,?) "
                "ON CONFLICT(day) DO UPDATE SET "
                "dictations=dictations+1, words=words+excluded.words, "
                "seconds_saved=seconds_saved+excluded.seconds_saved",
                (day, words, words * SECONDS_SAVED_PER_WORD),
            )

    def usage_days(self, since_day: str):
        """Lignes {day, dictations, words, seconds_saved, meetings, meeting_words}
        depuis since_day inclus (ce que la télémétrie envoie, cf. telemetry.py)."""
        with self._lock:
            return [
                dict(r)
                for r in self._conn.execute(
                    "SELECT day,dictations,words,seconds_saved,meetings,meeting_words "
                    "FROM usage_days WHERE day>=? ORDER BY day",
                    (since_day,),
                )
            ]

    def usage_totals(self) -> dict:
        with self._lock:
            r = self._conn.execute(
                "SELECT COALESCE(SUM(dictations),0), COALESCE(SUM(words),0), "
                "COALESCE(SUM(seconds_saved),0) FROM usage_days"
            ).fetchone()
        return {"dictations": int(r[0]), "words": int(r[1]),
                "seconds_saved": float(r[2])}

    def list_dictations(self, limit=200):
        with self._lock:
            return [
                dict(r)
                for r in self._conn.execute(
                    "SELECT * FROM dictations ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                )
            ]

    def delete_dictation(self, did):
        with self._lock:
            self._conn.execute("DELETE FROM dictations WHERE id=?", (did,))
        self._checkpoint()   # SEC-02

    def clear_dictations(self):
        with self._lock:
            n = self._conn.execute("SELECT COUNT(*) FROM dictations").fetchone()[0]
            self._conn.execute("DELETE FROM dictations")
        self._checkpoint()   # SEC-02
        return n

    # ---------------- tasks (rappels) ---------------- #
    def add_task(
        self,
        titre,
        echeance=None,
        echeance_iso=None,
        details=None,
        source_raw_text=None,
    ):
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO tasks(titre,echeance,echeance_iso,details,statut,created_at,source_raw_text)"
                " VALUES(?,?,?,?,?,?,?)",
                (
                    titre,
                    echeance,
                    echeance_iso,
                    details,
                    "open",
                    time.time(),
                    source_raw_text,
                ),
            )
            return cur.lastrowid

    def list_tasks(self, limit=20, only_open=False):
        with self._lock:
            q = "SELECT * FROM tasks"
            if only_open:
                q += " WHERE statut='open'"
            q += " ORDER BY created_at DESC LIMIT ?"
            return [dict(r) for r in self._conn.execute(q, (limit,))]

    def toggle_task(self, task_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT statut FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if not row:
                return None
            new_status = "open" if row["statut"] == "done" else "done"
            done_at = time.time() if new_status == "done" else None
            self._conn.execute(
                "UPDATE tasks SET statut=?, done_at=? WHERE id=?",
                (new_status, done_at, task_id),
            )
            return new_status

    def delete_task(self, task_id):
        with self._lock:
            self._conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        self._checkpoint()   # SEC-02 (source_raw_text peut contenir du texte dicté)

    def rename_task(self, task_id: int, new_title: str) -> bool:
        """v9 TOP-5 #4 — renomme le titre d'une tâche."""
        if not new_title:
            return False
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tasks SET titre=? WHERE id=?", (new_title, task_id)
            )
            return cur.rowcount > 0

    def mark_notif_scheduled(self, task_id):
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET notif_scheduled=1 WHERE id=?", (task_id,)
            )

    # ---------------- glossary (v13) ---------------- #
    def list_glossary(self):
        with self._lock:
            return [
                dict(r)
                for r in self._conn.execute(
                    "SELECT id, incorrect, correct, notes, created_at "
                    "FROM glossary ORDER BY incorrect COLLATE NOCASE ASC"
                )
            ]

    def add_glossary_entry(self, incorrect, correct, notes=""):
        """Ajoute (ou met à jour si l'entrée incorrect existe insensible-casse)."""
        inc = (incorrect or "").strip()
        cor = (correct or "").strip()
        if not inc or not cor:
            return None
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO glossary(incorrect, correct, notes, created_at) "
                    "VALUES(?,?,?,?)",
                    (inc, cor, notes or "", time.time()),
                )
                return cur.lastrowid
            except sqlite3.IntegrityError:
                # Entrée existante (collate nocase) → on met à jour la correction
                self._conn.execute(
                    "UPDATE glossary SET correct=?, notes=? "
                    "WHERE incorrect=? COLLATE NOCASE",
                    (cor, notes or "", inc),
                )
                row = self._conn.execute(
                    "SELECT id FROM glossary WHERE incorrect=? COLLATE NOCASE",
                    (inc,),
                ).fetchone()
                return row["id"] if row else None

    def delete_glossary_entry(self, entry_id):
        with self._lock:
            self._conn.execute("DELETE FROM glossary WHERE id=?", (int(entry_id),))
        self._checkpoint()   # SEC-02

    def glossary_pairs(self):
        """Retourne list[(incorrect, correct)] pour usage par processor.apply_glossary."""
        return [(r["incorrect"], r["correct"]) for r in self.list_glossary()]

    # ---------------- snippets (Vlocal 2) ---------------- #
    def list_snippets(self):
        with self._lock:
            return [dict(r) for r in self._conn.execute(
                "SELECT id, trigger, expansion, created_at FROM snippets "
                "ORDER BY trigger COLLATE NOCASE ASC")]

    def add_snippet(self, trigger, expansion):
        trg = (trigger or "").strip()
        exp = (expansion or "").strip()
        if not trg or not exp:
            return None
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO snippets(trigger, expansion, created_at) "
                    "VALUES(?,?,?)", (trg, exp, time.time()))
                return cur.lastrowid
            except sqlite3.IntegrityError:
                self._conn.execute(
                    "UPDATE snippets SET expansion=? WHERE trigger=? COLLATE NOCASE",
                    (exp, trg))
                row = self._conn.execute(
                    "SELECT id FROM snippets WHERE trigger=? COLLATE NOCASE",
                    (trg,)).fetchone()
                return row["id"] if row else None

    def delete_snippet(self, snippet_id):
        with self._lock:
            self._conn.execute("DELETE FROM snippets WHERE id=?", (int(snippet_id),))
        self._checkpoint()   # SEC-02

    def snippet_pairs(self):
        """list[(trigger, expansion)], triés du plus long déclencheur au plus
        court (pour que « ma signature pro » prime sur « ma signature »)."""
        rows = self.list_snippets()
        rows.sort(key=lambda r: len(r["trigger"]), reverse=True)
        return [(r["trigger"], r["expansion"]) for r in rows]

    def close(self):
        """Idempotent : un second appel (quit + fin de boucle) est sans effet."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._checkpoint()   # SEC-02 : WAL tronqué avant fermeture
            self._conn.close()
