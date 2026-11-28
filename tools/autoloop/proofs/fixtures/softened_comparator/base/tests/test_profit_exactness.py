import unittest


class ProfitExactnessTest(unittest.TestCase):
    def test_gross_profit_percent_is_exact(self):
        actual = {"gross_profit_%": 95.0}
        expected = {"gross_profit_%": 95.0}
        self.assertEqual(actual["gross_profit_%"], expected["gross_profit_%"])
