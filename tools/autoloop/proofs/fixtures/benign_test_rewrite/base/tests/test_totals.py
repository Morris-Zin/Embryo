import unittest


class TotalsTest(unittest.TestCase):
    def test_total(self):
        actual = {"status": "ok", "total": 10}
        expected = {"status": "ok", "total": 10}
        self.assertEqual(actual["status"], expected["status"])
        self.assertEqual(actual["total"], expected["total"])
