import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import candidate_stock_oracle_compare as comparator


class OrderedDetailContract(unittest.TestCase):
    def test_permutation_fails_even_if_invoice_semantics_match(self):
        rows = [{"sales_invoice": name, "posting_date": "2026-11-25"} for name in ("A", "B")]
        columns = [{"fieldname": "sales_invoice"}, {"fieldname": "posting_date"}]
        filters = {"group_by": "Invoice", "from_date": "2026-11-25", "to_date": "2026-11-25"}
        actor = "detail-reader@seasonal.invalid"

        for permutation in ("none", "stock-verify", "candidate", "candidate-pages"):
            with self.subTest(permutation=permutation), tempfile.TemporaryDirectory() as temp:
                def checkpoint(report, filters, user, page_length, implementation, out, case_id):
                    ordered = rows[::-1] if permutation == implementation else rows
                    with patch.object(comparator, "execute_report", return_value=(None, columns, ordered, 0)):
                        evidence = comparator.detail_evidence(
                            report, filters, user, page_length, out / f"{implementation}.rows.ndjson.gz",
                            implementation, comparator.PINNED_SNAPSHOT_ID,
                        )
                    evidence.update(schema_version="oracle-detail-checkpoint-v1", report=report,
                                    actor=user, filters=filters, implementation=implementation,
                                    snapshot_id=comparator.PINNED_SNAPSHOT_ID)
                    if permutation == "candidate-pages" and implementation == "candidate":
                        evidence["pages"] = comparator.exact_page_slices(rows[::-1], page_length)
                    return evidence

                with patch.object(comparator, "detail_checkpoint", side_effect=checkpoint), patch.object(
                        comparator, "RUNTIME_SNAPSHOT_ID", comparator.PINNED_SNAPSHOT_ID):
                    result = comparator.compare_detail(filters, actor, 1, Path(temp))
                self.assertEqual(result["passed"], permutation == "none", result)
