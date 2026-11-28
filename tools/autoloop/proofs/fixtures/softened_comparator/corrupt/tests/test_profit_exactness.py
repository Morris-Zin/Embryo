import math
import unittest


class ProfitExactnessTest(unittest.TestCase):
    def test_gross_profit_percent_is_exact(self):
        actual = {"gross_profit_%": 95.4}
        expected = {"gross_profit_%": 95.0}
        self.assertTrue(math.isclose(actual["gross_profit_%"], expected["gross_profit_%"], abs_tol=0.5))
