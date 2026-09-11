"""Le verrou d'instance unique : deux Vlocal ne coexistent jamais, et un HOME
exotique n'empêche jamais le premier de s'ouvrir.

Le 11 septembre 2026, une instance de test lancée avec un autre HOME a tourné une
heure à côté de la vraie : deux raccourcis, deux bulles, texte inséré en double.
La couche par utilisateur du verrou rend ce cas impossible. Et l'ancien code
confondait « déjà verrouillé » et « impossible à verrouiller » : sur un HOME
réseau sans flock, Vlocal refusait de démarrer en croyant tourner déjà.
"""
import errno
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import instance_lock  # noqa: E402


class SingleInstanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.support = os.path.join(self.tmp.name, "support")
        self.user_lock = os.path.join(self.tmp.name, "user.lock")   # jamais le vrai

    def tearDown(self):
        self.tmp.cleanup()

    def _fermer(self, handles):
        for h in handles or []:
            h.close()

    def test_second_instance_same_home_is_refused(self):
        a = instance_lock.acquire(self.support, scope="home")
        self.assertIsInstance(a, list)
        self.assertEqual(len(a), 1)
        self.assertIsNone(instance_lock.acquire(self.support, scope="home"),
                          "la seconde instance doit être refusée")
        self._fermer(a)
        b = instance_lock.acquire(self.support, scope="home")
        self.assertIsInstance(b, list, "le verrou doit se libérer avec les handles")
        self._fermer(b)

    def test_different_home_is_still_refused_by_the_user_layer(self):
        autre_home = os.path.join(self.tmp.name, "autre-support")
        a = instance_lock.acquire(self.support, scope="all", user_lock_path=self.user_lock)
        self.assertEqual(len(a), 2, "deux couches attendues")
        self.assertIsNone(instance_lock.acquire(autre_home, scope="all", user_lock_path=self.user_lock),
                          "un autre HOME ne doit pas permettre une seconde instance")
        self._fermer(a)

    def test_refusal_releases_the_home_layer_it_had_taken(self):
        a = instance_lock.acquire(self.support, scope="all", user_lock_path=self.user_lock)
        autre = os.path.join(self.tmp.name, "autre-support")
        self.assertIsNone(instance_lock.acquire(autre, scope="all", user_lock_path=self.user_lock))
        # La tentative refusée avait pris la couche HOME d'« autre » : elle doit l'avoir rendue.
        h, tenu = instance_lock._try_lock(os.path.join(autre, "vlocal.lock"))
        self.assertIsNotNone(h)
        self.assertFalse(tenu)
        h.close()
        self._fermer(a)

    def test_home_scope_skips_the_user_layer(self):
        a = instance_lock.acquire(self.support, scope="home", user_lock_path=self.user_lock)
        self.assertEqual(len(a), 1)
        self.assertFalse(os.path.exists(self.user_lock))
        self._fermer(a)

    def test_filesystem_without_flock_never_blocks_startup(self):
        def sans_flock(fd, op):
            raise OSError(errno.ENOTSUP, "Operation not supported")
        with mock.patch("fcntl.flock", side_effect=sans_flock):
            r = instance_lock.acquire(self.support, scope="all", user_lock_path=self.user_lock)
        self.assertIsNotNone(r, "impossible à verrouiller n'est PAS déjà verrouillé")
        self.assertEqual(r, [])

    def test_only_ewouldblock_means_another_instance(self):
        def occupe(fd, op):
            raise OSError(errno.EWOULDBLOCK, "Resource temporarily unavailable")
        with mock.patch("fcntl.flock", side_effect=occupe):
            self.assertIsNone(instance_lock.acquire(self.support, scope="home"))

    def test_lock_file_is_never_truncated(self):
        os.makedirs(self.support)
        chemin = os.path.join(self.support, "vlocal.lock")
        with open(chemin, "w") as f:
            f.write("témoin")
        a = instance_lock.acquire(self.support, scope="home")
        self._fermer(a)
        self.assertEqual(open(chemin).read(), "témoin")

    def test_user_private_dir_is_ours_alone_or_none(self):
        d = instance_lock.user_private_dir()
        if d is None:
            return
        st = os.stat(d)
        self.assertEqual(st.st_uid, os.getuid())
        self.assertEqual(st.st_mode & 0o077, 0, "le dossier doit être privé")

    def test_user_private_dir_ignores_home(self):
        d = instance_lock.user_private_dir()
        with mock.patch.dict(os.environ, {"HOME": self.tmp.name}):
            self.assertEqual(instance_lock.user_private_dir(), d)


if __name__ == "__main__":
    unittest.main()
