"""v1.4.2 : IPv4 d'abord (reseau.py)."""
import os
import socket
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import reseau  # noqa: E402


class TestIPv4DAbord(unittest.TestCase):
    def test_ipv4_trie_en_premier_ipv6_garde(self):
        faux = [(socket.AF_INET6, 1, 6, "", ("2606::1", 443, 0, 0)), (socket.AF_INET, 1, 6, "", ("104.18.50.34", 443))]
        orig = reseau._ORIGINAL
        try:
            reseau._ORIGINAL = lambda *a, **k: list(faux)
            res = reseau._ipv4_d_abord("exemple", 443)
        finally:
            reseau._ORIGINAL = orig
        self.assertEqual([r[0] for r in res], [socket.AF_INET, socket.AF_INET6])

    def test_idempotent(self):
        reseau.appliquer()
        reseau.appliquer()
        self.assertIs(socket.getaddrinfo, reseau._ipv4_d_abord)


if __name__ == "__main__":
    unittest.main()
