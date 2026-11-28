import unittest

import verify_original_export_csv as verifier


class ExportColumnTests(unittest.TestCase):
    def test_label_fallback_preserves_values_and_display_headers(self):
        columns = [{"label": "Brand"}, {"label": "Net Unit-Price"}, {"label": "Alias", "fieldname": "explicit"}, {}]
        fields = verifier.column_fieldnames(columns)
        self.assertEqual(fields, ["brand", "net_unit_price", "explicit", "3"])
        self.assertEqual(verifier.column_headers(columns), ["Brand", "Net Unit-Price", "Alias", ""])
        row = {"brand": "Phsar Select", "net_unit_price": 7, "explicit": 1, "3": "x"}
        self.assertEqual(verifier.serialize_csv_record(verifier.row_values(row, fields)), b"Phsar Select,7,1,x\r\n")
