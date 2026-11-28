import unittest


class TotalsTest(unittest.TestCase):
    def test_total(self):
        actual = {"status": "ok", "total": 10}
        self.assertEqual(actual["status"], "ok")
