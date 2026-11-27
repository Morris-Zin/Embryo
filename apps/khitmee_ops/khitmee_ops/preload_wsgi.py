"""Gunicorn entrypoint that prewarms the item-search catalog in the master process.

With ``--preload`` gunicorn imports this module in the master before forking
workers, so every worker inherits the built catalog copy-on-write. A recycled
worker then serves warm searches immediately instead of paying the cold catalog
rebuild (~400ms) on its first search.

Opt-in via ``KHITMEE_PREWARM_CATALOG=1``; any failure leaves a normal cold-start
master so worker boot is never at risk. Database and cache connections opened
for the prewarm are closed before workers fork.
"""

import os
import traceback

import frappe  # noqa: F401  (imported in master with --preload)

try:
	from frappe.app import application  # noqa: F401  (gunicorn target)
except Exception:  # pragma: no cover - only outside a bench (unit tests)
	application = None

_ENABLED_VALUES = {"1", "true", "yes", "on"}


def _prewarm_enabled():
	return str(os.environ.get("KHITMEE_PREWARM_CATALOG", "") or "").strip().lower() in _ENABLED_VALUES


def _close_master_connections():
	try:
		db = getattr(frappe, "db", None)
		close = getattr(db, "close", None)
		if callable(close):
			close()
	except Exception:
		pass
	try:
		cache = getattr(frappe, "cache", None)
		if callable(cache):
			cache = cache()
		pool = getattr(cache, "connection_pool", None)
		disconnect = getattr(pool, "disconnect", None)
		if callable(disconnect):
			disconnect()
	except Exception:
		pass
	try:
		frappe.destroy()
	except Exception:
		pass


def _site_dirs(sites_path):
	try:
		entries = sorted(os.listdir(sites_path))
	except OSError:
		return []
	sites = []
	for entry in entries:
		if entry.startswith(".") or entry in ("assets",):
			continue
		path = os.path.join(sites_path, entry)
		if os.path.isdir(path) and os.path.exists(os.path.join(path, "site_config.json")):
			sites.append(entry)
	return sites


def prewarm_item_catalogs(sites_path=None):
	"""Build the per-site item-search catalog in the master; returns warmed site count."""
	if not _prewarm_enabled():
		return 0
	try:
		from khitmee_ops.api import search
	except Exception:
		return 0

	sites_path = sites_path or os.getcwd()
	warmed = 0
	for site in _site_dirs(sites_path):
		try:
			frappe.init(site=site, sites_path=sites_path)
			frappe.connect()
			search._get_item_catalog()
			warmed += 1
		except Exception:
			traceback.print_exc()
		finally:
			_close_master_connections()
	return warmed


prewarm_item_catalogs()
