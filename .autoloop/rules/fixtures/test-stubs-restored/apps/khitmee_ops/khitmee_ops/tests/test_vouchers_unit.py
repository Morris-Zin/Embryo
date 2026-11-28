import unittest
import frappe


class TestVoucherStub(unittest.TestCase):
    def setUp(self):
        frappe.throw = lambda message: None
