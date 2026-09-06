#!/usr/bin/env python3
"""Vlocal — tests CRUD storage (sandbox-friendly, DB temporaire)."""

import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from storage import Store


def main():
    print("=== storage CRUD ===")
    with tempfile.TemporaryDirectory() as d:
        s = Store(os.path.join(d, "vlocal.db"))

        # tasks (rappels)
        t1 = s.add_task(
            "Rappeler comptable",
            echeance="demain avant midi",
            echeance_iso="2026-06-07T11:00:00",
            details="TVA + factures de mai",
            source_raw_text="raw tâche",
        )
        t2 = s.add_task("Acheter du pain")
        assert t1 > 0 and t2 > t1

        # toggle
        assert s.toggle_task(t1) == "done"
        assert s.toggle_task(t1) == "open"
        s.toggle_task(t1)  # done
        only_open = s.list_tasks(only_open=True)
        assert all(t["statut"] == "open" for t in only_open)
        assert any(t["id"] == t2 for t in only_open) and not any(
            t["id"] == t1 for t in only_open
        )

        # notif scheduled flag
        s.mark_notif_scheduled(t1)
        row = [t for t in s.list_tasks() if t["id"] == t1][0]
        assert row["notif_scheduled"] == 1

        # delete
        s.delete_task(t1)
        s.delete_task(t2)
        assert len(s.list_tasks()) == 0

        # accents/unicode round-trip
        s.add_task("Préparer l'élève — œuvre française")
        assert "œuvre" in s.list_tasks()[0]["titre"]

        # multi-threads (sanity check : pas de deadlock)
        errors = []

        def writer(i):
            try:
                for _ in range(20):
                    s.add_task(f"thread {i}")
            except Exception as e:
                errors.append(e)

        ts = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert not errors, errors
        assert len(s.list_tasks(limit=1000)) >= 80

        # idempotence des migrations : ré-ouvrir = pas de doublon de schéma
        s.close()
        s2 = Store(os.path.join(d, "vlocal.db"))
        assert s2._conn.execute("PRAGMA user_version").fetchone()[0] >= 1
        s2.close()

    print("[OK] storage : CRUD, toggle, filtres, unicode, threads, migrations idempotentes")


if __name__ == "__main__":
    main()
