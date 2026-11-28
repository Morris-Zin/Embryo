import unittest


def assert_totals_match(case, actual, expected):
    case.assertEqual(actual["status"], expected["status"])
    case.assertEqual(actual["total"], expected["total"])


class TotalsTest(unittest.TestCase):
    def test_totals_match(self):
        actual = {"status": "ok", "total": 10}
        expected = {"status": "ok", "total": 10}
        assert_totals_match(self, actual, expected)
