"""Bounded whole-invoice replay orchestration for Set B candidates."""

from __future__ import annotations


def empty_contribution_batch(currency_precision=3, float_precision=2, rounding_method="Banker's Rounding (legacy)"):
	return {
		"contributions": [],
		"replay_reasons_by_invoice": {},
		"currency_precision": int(currency_precision),
		"float_precision": int(float_precision),
		"rounding_method": rounding_method,
	}


def _fast_gross_profit_module():
	"""Lazy import keeps Worker 2/4 circularity-free and lets tests inject fakes."""
	from khitmee_ops.khitmee_ops.report.fast_gross_profit import fast_gross_profit

	return fast_gross_profit


def extract_replay_invoice_ids(replay_rows) -> list[str]:
	"""Return deterministic unique candidate invoice ids."""
	return sorted({row.get("sales_invoice") for row in replay_rows or [] if row.get("sales_invoice")})


def collect_replay_contributions(filters: dict, invoice_ids) -> dict:
	"""Replay Set B invoice families under the complete user request.

	`invoice_ids=[]` is an immediate empty return with zero database queries. The
	extractor resolves visible main rows, suppression and in-date return details
	with the complete filters and the session actor's visibility.
	"""
	ordered_ids = tuple(sorted(dict.fromkeys(invoice_ids or ())))
	if not ordered_ids:
		return empty_contribution_batch()
	return _fast_gross_profit_module().collect_profit_contributions(filters or {}, invoice_ids=ordered_ids)


def merge_contribution_batches(*batches) -> dict:
	"""Merge contribution batches without concatenating precision metadata ambiguously."""
	merged = empty_contribution_batch()
	first = next((batch for batch in batches if batch), None)
	if first:
		merged.update(
			{
				"currency_precision": int(first.get("currency_precision") or 3),
				"float_precision": int(first.get("float_precision") or 2),
				"rounding_method": first.get("rounding_method") or merged["rounding_method"],
			}
		)
	seen_rows = set()
	for batch in batches:
		if not batch:
			continue
		for row in batch.get("contributions") or []:
			identity = (
				row.get("sales_invoice"),
				row.get("item_row"),
				row.get("query_phase"),
				row.get("first_seen_key"),
			)
			if identity in seen_rows:
				continue
			seen_rows.add(identity)
			merged["contributions"].append(row)
		for invoice, reason in (batch.get("replay_reasons_by_invoice") or {}).items():
			merged["replay_reasons_by_invoice"].setdefault(invoice, reason)
	return merged


def largest_replay_family_rows(batch) -> int:
	"""Count rows in the largest replayed invoice family visible in a contribution batch."""
	counts = {}
	for row in (batch or {}).get("contributions") or []:
		invoice = row.get("sales_invoice")
		if invoice:
			counts[invoice] = counts.get(invoice, 0) + 1
	return max(counts.values(), default=0)
