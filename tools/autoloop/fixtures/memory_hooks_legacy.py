"""Source fixture for the legacy stub baseline. Parsed by the rule tests."""

import sys
import types
import unittest
from types import SimpleNamespace

from khitmee_ops.api import memory


def install_frappe_stub():
    frappe = sys.modules.get("frappe") or types.ModuleType("frappe")
    frappe.form_dict = {}
    frappe.flags = SimpleNamespace()
    sys.modules["frappe"] = frappe


install_frappe_stub()


class TestMaybeTrimHeap(unittest.TestCase):
    def setUp(self):
        memory.frappe.form_dict = {"cmd": "frappe.desk.query_report.run"}
        memory.frappe.flags = SimpleNamespace()
        memory.frappe.local = SimpleNamespace(response={})
