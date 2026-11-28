def _row_to_dict(columns, row):
    fieldnames = [col.get("fieldname") for col in columns]
    return dict(zip(fieldnames, row))


def compare_financial_reports(columns, rows):
    return [_row_to_dict(columns, row) for row in rows]
