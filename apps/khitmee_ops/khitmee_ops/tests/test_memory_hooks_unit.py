import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

APP_ROOT = Path(__file__).resolve().parents[2]
if str(APP_ROOT) not in sys.path:
	sys.path.insert(0, str(APP_ROOT))


def install_frappe_stub():
	frappe = sys.modules.get("frappe") or types.ModuleType("frappe")
	frappe.form_dict = {}
	frappe.flags = SimpleNamespace()
	sys.modules["frappe"] = frappe


install_frappe_stub()

from khitmee_ops import hooks
from khitmee_ops.api import memory


class Response:
	def __init__(self, size, use_content_length=True):
		self.body = b"x" * size
		self.headers = {"Content-Length": str(size)} if use_content_length else {}

	def get_data(self, as_text=False):
		return self.body


class AfterResponse:
	def __init__(self):
		self.callbacks = []

	def add(self, callback):
		self.callbacks.append(callback)


class TestMaybeTrimHeap(unittest.TestCase):
	def setUp(self):
		memory.frappe.form_dict = {"cmd": "frappe.desk.query_report.run"}
		memory.frappe.flags = SimpleNamespace()
		memory.frappe.local = SimpleNamespace(response={})
		memory._RECYCLE_REQUESTED = False

	def test_large_report_response_trims_heap_once(self):
		calls = []
		with patch.object(memory, "_malloc_trim", return_value=lambda amount: calls.append(amount)):
			self.assertTrue(memory.maybe_trim_heap(Response(memory.HEAVY_REPORT_THRESHOLD_BYTES)))
		self.assertEqual(calls, [0])

	def test_small_report_response_and_non_report_response_do_not_trim(self):
		with patch.object(memory, "_malloc_trim", side_effect=AssertionError("must not load libc")):
			self.assertFalse(memory.maybe_trim_heap(Response(memory.HEAVY_REPORT_THRESHOLD_BYTES - 1)))
			memory.frappe.form_dict = {"cmd": "other.method"}
			self.assertFalse(memory.maybe_trim_heap(Response(memory.HEAVY_REPORT_THRESHOLD_BYTES)))

	def test_explicit_large_report_flag_trims_even_without_content_length(self):
		memory.frappe.flags.khitmee_large_report_response = True
		calls = []
		with patch.object(memory, "_malloc_trim", return_value=lambda amount: calls.append(amount)):
			self.assertTrue(memory.maybe_trim_heap(Response(128, use_content_length=False)))
		self.assertEqual(calls, [0])

	def test_untrusted_form_dict_large_report_trigger_is_ignored(self):
		memory.frappe.form_dict = {
			"cmd": "other.method",
			"khitmee_large_report_response": True,
		}
		with patch.object(memory, "_malloc_trim", side_effect=AssertionError("form_dict must not force trim")):
			self.assertFalse(memory.maybe_trim_heap(Response(128, use_content_length=False)))

	def test_after_response_defers_message_pop_and_malloc_trim(self):
		memory.frappe.flags.khitmee_large_report_response = True
		memory.frappe.local.response = {"message": {"large": "payload"}}
		after_response = AfterResponse()
		request = SimpleNamespace(after_response=after_response)
		calls = []

		with patch.object(memory, "_malloc_trim", return_value=lambda amount: calls.append(amount)):
			self.assertTrue(memory.maybe_trim_heap(Response(128, use_content_length=False), request=request))
			self.assertEqual(calls, [])
			self.assertIn("message", memory.frappe.local.response)
			self.assertEqual(len(after_response.callbacks), 1)

			after_response.callbacks[0]()

		self.assertEqual(calls, [0])
		self.assertNotIn("message", memory.frappe.local.response)

	def test_missing_libc_and_trim_errors_fail_silently(self):
		with patch.object(memory, "_malloc_trim", return_value=None):
			self.assertFalse(memory.maybe_trim_heap(Response(memory.HEAVY_REPORT_THRESHOLD_BYTES)))
		with patch.object(memory, "_malloc_trim", side_effect=OSError("no libc")):
			self.assertFalse(memory.maybe_trim_heap(Response(memory.HEAVY_REPORT_THRESHOLD_BYTES)))


class TestWorkerRecycle(unittest.TestCase):
	def setUp(self):
		memory.frappe.form_dict = {"cmd": "frappe.desk.query_report.run"}
		memory.frappe.flags = SimpleNamespace()
		memory.frappe.local = SimpleNamespace(response={})
		memory._RECYCLE_REQUESTED = False

	def test_current_rss_bytes_reads_proc_statm(self):
		self.assertGreater(memory._current_rss_bytes(), 0)

	def test_no_threshold_never_recycles(self):
		with patch.dict("os.environ", {}, clear=False), patch.object(
			memory.os, "kill", side_effect=AssertionError("must not signal")
		):
			memory.os.environ.pop(memory.WORKER_RECYCLE_RSS_ENV, None)
			self.assertFalse(memory._recycle_worker_if_needed())

	def test_below_threshold_does_not_recycle(self):
		with patch.dict("os.environ", {memory.WORKER_RECYCLE_RSS_ENV: "100000"}), patch.object(
			memory.os, "kill", side_effect=AssertionError("must not signal")
		):
			self.assertFalse(memory._recycle_worker_if_needed())

	def test_over_threshold_recycles_exactly_once(self):
		kills = []
		with patch.dict("os.environ", {memory.WORKER_RECYCLE_RSS_ENV: "0.0001"}), patch.object(
			memory.os, "kill", lambda pid, sig: kills.append((pid, sig))
		):
			self.assertTrue(memory._recycle_worker_if_needed())
			self.assertFalse(memory._recycle_worker_if_needed())
		import os as real_os
		import signal as real_signal
		self.assertEqual(kills, [(real_os.getpid(), real_signal.SIGTERM)])

	def test_recycle_triggered_from_heavy_report_release(self):
		memory.frappe.flags.khitmee_large_report_response = True
		kills = []
		with patch.dict("os.environ", {memory.WORKER_RECYCLE_RSS_ENV: "0.0001"}), patch.object(
			memory, "_malloc_trim", return_value=lambda amount: None
		), patch.object(memory.os, "kill", lambda pid, sig: kills.append(sig)):
			self.assertTrue(memory.maybe_trim_heap(Response(128, use_content_length=False)))
		self.assertEqual(len(kills), 1)


class TestPrewarmModule(unittest.TestCase):
	def test_prewarm_disabled_by_default(self):
		import importlib

		prewarm = importlib.import_module("khitmee_ops.preload_wsgi")
		with patch.dict("os.environ", {}, clear=True):
			self.assertEqual(prewarm.prewarm_item_catalogs(sites_path="/nonexistent"), 0)

	def test_prewarm_enabled_skips_missing_sites(self):
		import importlib

		prewarm = importlib.import_module("khitmee_ops.preload_wsgi")
		with patch.dict("os.environ", {"KHITMEE_PREWARM_CATALOG": "1"}, clear=True):
			self.assertEqual(prewarm.prewarm_item_catalogs(sites_path="/nonexistent"), 0)

	def test_site_dirs_requires_site_config(self):
		import importlib
		import tempfile

		prewarm = importlib.import_module("khitmee_ops.preload_wsgi")
		with tempfile.TemporaryDirectory() as tmp:
			import os as real_os
			real_os.makedirs(real_os.path.join(tmp, "site1.localhost"))
			real_os.makedirs(real_os.path.join(tmp, "assets"))
			self.assertEqual(prewarm._site_dirs(tmp), [])
			open(real_os.path.join(tmp, "site1.localhost", "site_config.json"), "w").write("{}")
			self.assertEqual(prewarm._site_dirs(tmp), ["site1.localhost"])


class TestHookWiring(unittest.TestCase):
	def test_hooks_wire_autopay_invalidation_and_after_request_exactly(self):
		self.assertEqual(
			hooks.doc_events["Sales Invoice"]["validate"],
			"khitmee_ops.api.vouchers.sync_counter_autopay_payment",
		)
		item_events = hooks.doc_events["Item"]
		for event in ("after_insert", "on_update", "on_trash", "after_rename"):
			self.assertEqual(item_events[event], "khitmee_ops.api.search.invalidate_item_search_cache")
		self.assertEqual(hooks.after_request, ["khitmee_ops.api.memory.maybe_trim_heap"])


if __name__ == "__main__":
	unittest.main()
