use crate::format_thermal::{format_money, format_qty};
use crate::models::ReceiptPayload;
use std::fmt::Write as _;

pub fn render_a4_html(payload: &ReceiptPayload) -> String {
    let mut rows = String::new();
    for (index, line) in payload.lines.iter().enumerate() {
        let _ = write!(
            rows,
            concat!(
                "<tr>",
                "<td class=\"no\">{}</td>",
                "<td>{}</td>",
                "<td class=\"num\">{}</td>",
                "<td class=\"num\">{}</td>",
                "<td class=\"num\">{}</td>",
                "</tr>"
            ),
            index + 1,
            escape_html(&line.name),
            escape_html(&format_qty(line.qty)),
            escape_html(&format_money(line.rate)),
            escape_html(&format_money(line.amount)),
        );
    }

    format!(
        r##"<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{voucher_no} - {title}</title>
<style>
@page {{ size: A4; margin: 16mm; }}
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; padding: 0; }}
body {{ font-family: Arial, Helvetica, sans-serif; color: #111827; font-size: 12pt; line-height: 1.45; }}
.document {{ max-width: 178mm; margin: 0 auto; }}
.header {{ text-align: center; border-bottom: 2px solid #111827; padding-bottom: 5mm; margin-bottom: 5mm; }}
.shop {{ font-size: 17pt; font-weight: 700; margin-bottom: 1.5mm; }}
.title {{ font-size: 13pt; font-weight: 700; }}
.voucher {{ margin-top: 1.5mm; color: #374151; }}
.meta {{ display: table; width: 100%; margin-bottom: 5mm; }}
.meta-row {{ display: table-row; }}
.meta-label {{ display: table-cell; width: 30mm; font-weight: 700; padding: 1mm 2mm 1mm 0; }}
.meta-value {{ display: table-cell; padding: 1mm 0; }}
table.items {{ width: 100%; border-collapse: collapse; margin-top: 4mm; }}
.items th, .items td {{ border: 1px solid #9ca3af; padding: 2.2mm 2.5mm; vertical-align: top; }}
.items th {{ background: #f3f4f6; font-weight: 700; text-align: left; }}
.items th.num, .items td.num {{ text-align: right; white-space: nowrap; }}
.items td.no {{ width: 10mm; text-align: center; }}
.total-row td {{ font-weight: 700; font-size: 13pt; background: #f9fafb; }}
.footer-note {{ margin-top: 6mm; white-space: pre-wrap; }}
.signatures {{ display: table; width: 100%; margin-top: 24mm; table-layout: fixed; }}
.signature {{ display: table-cell; text-align: center; padding: 0 8mm; }}
.signature-line {{ border-top: 1px solid #111827; margin-bottom: 2mm; height: 1px; }}
.signature-label {{ font-size: 10pt; color: #374151; }}
@media print {{ body {{ print-color-adjust: exact; -webkit-print-color-adjust: exact; }} }}
</style>
</head>
<body>
<main class="document">
  <header class="header">
    <div class="shop">{shop_name}</div>
    <div class="title">{title}</div>
    <div class="voucher">Voucher No: {voucher_no}</div>
  </header>
  <section class="meta" aria-label="Voucher details">
    <div class="meta-row"><div class="meta-label">Date</div><div class="meta-value">{date}</div></div>
    <div class="meta-row"><div class="meta-label">Customer</div><div class="meta-value">{party}</div></div>
  </section>
  <table class="items">
    <thead>
      <tr><th class="no">#</th><th>Item</th><th class="num">Qty</th><th class="num">Rate</th><th class="num">Amount</th></tr>
    </thead>
    <tbody>
      {rows}
      <tr class="total-row"><td colspan="4">TOTAL</td><td class="num">{total}</td></tr>
    </tbody>
  </table>
  <p class="footer-note">{footer}</p>
  <section class="signatures" aria-label="Signatures">
    <div class="signature"><div class="signature-line"></div><div class="signature-label">Prepared by</div></div>
    <div class="signature"><div class="signature-line"></div><div class="signature-label">Checked by</div></div>
    <div class="signature"><div class="signature-line"></div><div class="signature-label">Received by</div></div>
  </section>
</main>
</body>
</html>
"##,
        voucher_no = escape_html(&payload.voucher_no),
        title = escape_html(&payload.title),
        shop_name = escape_html(&payload.shop_name),
        date = escape_html(&payload.date),
        party = escape_html(&payload.party),
        rows = rows,
        total = escape_html(&format_money(payload.total)),
        footer = escape_html(&payload.footer),
    )
}

fn escape_html(text: &str) -> String {
    let mut escaped = String::with_capacity(text.len());
    for ch in text.chars() {
        match ch {
            '&' => escaped.push_str("&amp;"),
            '<' => escaped.push_str("&lt;"),
            '>' => escaped.push_str("&gt;"),
            '"' => escaped.push_str("&quot;"),
            '\'' => escaped.push_str("&#39;"),
            _ => escaped.push(ch),
        }
    }
    escaped
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::models::{PrinterType, ReceiptLine};

    fn sample_payload() -> ReceiptPayload {
        ReceiptPayload {
            printer_type: PrinterType::A4,
            voucher_no: "SINV-0001".to_string(),
            title: "Khit Me Sales Voucher".to_string(),
            shop_name: "Dist Hub 1".to_string(),
            date: "2026-11-26 16:00:00".to_string(),
            party: "Walk-in Customer".to_string(),
            lines: vec![ReceiptLine {
                name: "Product Sample A".to_string(),
                qty: 5.0,
                rate: 11760.0,
                amount: 58800.0,
            }],
            total: 58800.0,
            footer: "Thank you!".to_string(),
        }
    }

    #[test]
    fn renders_self_contained_a4_html() {
        let html = render_a4_html(&sample_payload());
        assert!(html.contains("@page { size: A4;"));
        assert!(html.contains("<table class=\"items\">"));
        assert!(html.contains("Product Sample A"));
        assert!(html.contains("58,800"));
        assert!(html.contains("Prepared by"));
        assert!(html.contains("Received by"));
        assert!(!html.contains("http://"));
        assert!(!html.contains("https://"));
    }

    #[test]
    fn escapes_all_dynamic_html_fields() {
        let mut payload = sample_payload();
        payload.shop_name = "<script>alert('x')</script>".to_string();
        payload.lines[0].name = "A&B <b>".to_string();
        payload.footer = "\"quoted\" & <tagged>".to_string();
        let html = render_a4_html(&payload);

        assert!(!html.contains("<script>alert"));
        assert!(html.contains("&lt;script&gt;alert(&#39;x&#39;)&lt;/script&gt;"));
        assert!(html.contains("A&amp;B &lt;b&gt;"));
        assert!(html.contains("&quot;quoted&quot; &amp; &lt;tagged&gt;"));
    }

    #[test]
    fn renders_one_table_row_per_receipt_line() {
        let mut payload = sample_payload();
        payload.lines.push(ReceiptLine {
            name: "Second item".to_string(),
            qty: 1.0,
            rate: 500.0,
            amount: 500.0,
        });
        let html = render_a4_html(&payload);
        assert!(html.contains("Second item"));
        assert_eq!(html.matches("<tr><td").count(), 2);
    }
}
