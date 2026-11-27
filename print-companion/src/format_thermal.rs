use crate::models::ReceiptPayload;

const ESC: u8 = 0x1b;
const GS: u8 = 0x1d;
const MIN_WIDTH: usize = 32;
const WIDE_LAYOUT_MIN_WIDTH: usize = 42;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Align {
    Left,
    Center,
}

#[derive(Debug, Clone)]
struct StyledLine {
    text: String,
    align: Align,
    bold: bool,
}

impl StyledLine {
    fn left(text: impl Into<String>) -> Self {
        Self {
            text: text.into(),
            align: Align::Left,
            bold: false,
        }
    }

    fn center(text: impl Into<String>, bold: bool) -> Self {
        Self {
            text: text.into(),
            align: Align::Center,
            bold,
        }
    }

    fn bold_left(text: impl Into<String>) -> Self {
        Self {
            text: text.into(),
            align: Align::Left,
            bold: true,
        }
    }
}

pub fn render_escpos(payload: &ReceiptPayload, paper_width_chars: usize) -> Vec<u8> {
    let width = normalized_width(paper_width_chars);
    let lines = build_lines(payload, width);
    let mut out = Vec::new();
    out.extend_from_slice(&[ESC, b'@']);
    out.extend_from_slice(&[ESC, b't', 0]);

    for line in lines {
        out.extend_from_slice(&[ESC, b'a', if line.align == Align::Center { 1 } else { 0 }]);
        out.extend_from_slice(&[ESC, b'E', if line.bold { 1 } else { 0 }]);
        out.extend_from_slice(ascii_fallback(&line.text).as_bytes());
        out.push(b'\n');
    }

    out.extend_from_slice(&[ESC, b'E', 0, ESC, b'a', 0]);
    out.extend_from_slice(&[b'\n', b'\n', b'\n']);
    out.extend_from_slice(&[GS, b'V', 1]);
    out
}

pub fn render_text_preview(payload: &ReceiptPayload, paper_width_chars: usize) -> String {
    let width = normalized_width(paper_width_chars);
    let lines = build_lines(payload, width);
    let mut out = String::new();

    for line in lines {
        let text = ascii_fallback(&line.text);
        let text = if line.align == Align::Center {
            center_text(&text, width)
        } else {
            text
        };
        out.push_str(&text);
        out.push('\n');
    }

    out.push_str("\n\n[partial cut]\n");
    out
}

pub(crate) fn format_money(value: f64) -> String {
    let negative = value.is_sign_negative() && value != 0.0;
    let absolute = value.abs();
    let rounded_cents = (absolute * 100.0).round() as u64;
    let sign = if negative { "-" } else { "" };

    if rounded_cents % 100 == 0 {
        format!("{}{}", sign, group_integer(rounded_cents / 100))
    } else {
        format!(
            "{}{}.{:02}",
            sign,
            group_integer(rounded_cents / 100),
            rounded_cents % 100
        )
    }
}

pub(crate) fn format_qty(value: f64) -> String {
    if value.fract().abs() < 0.0000001 {
        format!("{}", value.round() as i64)
    } else {
        let mut text = format!("{:.3}", value);
        while text.ends_with('0') {
            text.pop();
        }
        if text.ends_with('.') {
            text.pop();
        }
        text
    }
}

fn build_lines(payload: &ReceiptPayload, width: usize) -> Vec<StyledLine> {
    let mut lines = Vec::new();
    lines.push(StyledLine::center(payload.shop_name.trim(), true));
    lines.push(StyledLine::center(payload.title.trim(), true));
    lines.push(StyledLine::center(payload.voucher_no.trim(), false));
    lines.push(StyledLine::left(separator(width)));
    lines.push(StyledLine::left(two_columns(
        "Date",
        payload.date.trim(),
        width,
    )));
    lines.push(StyledLine::left(two_columns(
        "Customer",
        payload.party.trim(),
        width,
    )));
    lines.push(StyledLine::left(separator(width)));

    if width >= WIDE_LAYOUT_MIN_WIDTH {
        lines.push(StyledLine::bold_left(wide_item_row(
            "ITEM", "QTY", "RATE", "AMOUNT", width,
        )));
        lines.push(StyledLine::left(separator(width)));
        for item in &payload.lines {
            push_wide_item(&mut lines, item, width);
        }
    } else {
        lines.push(StyledLine::bold_left(two_columns("ITEM", "AMOUNT", width)));
        lines.push(StyledLine::left(separator(width)));
        for item in &payload.lines {
            push_narrow_item(&mut lines, item, width);
        }
    }

    lines.push(StyledLine::left(separator(width)));
    lines.push(StyledLine::bold_left(two_columns(
        "TOTAL",
        &format_money(payload.total),
        width,
    )));
    lines.push(StyledLine::left(separator(width)));

    for footer_line in wrap_text(payload.footer.trim(), width) {
        lines.push(StyledLine::center(footer_line, false));
    }

    lines
}

fn push_wide_item(lines: &mut Vec<StyledLine>, item: &crate::models::ReceiptLine, width: usize) {
    let name_width = wide_name_width(width);
    let wrapped = wrap_text(&item.name, name_width);
    let qty = format_qty(item.qty);
    let rate = format_money(item.rate);
    let amount = format_money(item.amount);

    for (index, part) in wrapped.iter().enumerate() {
        if index == 0 {
            lines.push(StyledLine::left(wide_item_row(
                part, &qty, &rate, &amount, width,
            )));
        } else {
            lines.push(StyledLine::left(wide_item_row(part, "", "", "", width)));
        }
    }
}

fn push_narrow_item(lines: &mut Vec<StyledLine>, item: &crate::models::ReceiptLine, width: usize) {
    let wrapped = wrap_text(&item.name, width);
    let amount = format_money(item.amount);
    for (index, part) in wrapped.iter().enumerate() {
        if index == 0 {
            lines.push(StyledLine::left(two_columns(part, &amount, width)));
        } else {
            lines.push(StyledLine::left(part.clone()));
        }
    }
    lines.push(StyledLine::left(format!(
        "  {} x {}",
        format_qty(item.qty),
        format_money(item.rate)
    )));
}

fn normalized_width(width: usize) -> usize {
    width.max(MIN_WIDTH)
}

fn separator(width: usize) -> String {
    "-".repeat(width)
}

fn wide_name_width(width: usize) -> usize {
    width.saturating_sub(5 + 10 + 12 + 3).max(8)
}

fn wide_item_row(name: &str, qty: &str, rate: &str, amount: &str, width: usize) -> String {
    let qty_width = 5;
    let rate_width = 10;
    let amount_width = 12;
    let name_width = wide_name_width(width);
    let mut row = String::new();
    row.push_str(&pad_right(&truncate_chars(name, name_width), name_width));
    row.push(' ');
    row.push_str(&pad_left(&truncate_chars(qty, qty_width), qty_width));
    row.push(' ');
    row.push_str(&pad_left(&truncate_chars(rate, rate_width), rate_width));
    row.push(' ');
    row.push_str(&pad_left(&truncate_chars(amount, amount_width), amount_width));
    row
}

fn two_columns(left: &str, right: &str, width: usize) -> String {
    let right_len = display_len(right);
    if right_len >= width {
        return truncate_chars(right, width);
    }
    let left_width = width.saturating_sub(right_len + 1);
    let left_part = pad_right(&truncate_chars(left, left_width), left_width);
    format!("{} {}", left_part, right)
}

fn center_text(text: &str, width: usize) -> String {
    let len = display_len(text);
    if len >= width {
        return truncate_chars(text, width);
    }
    let left = (width - len) / 2;
    format!("{}{}", " ".repeat(left), text)
}

fn wrap_text(text: &str, width: usize) -> Vec<String> {
    if text.trim().is_empty() {
        return vec![String::new()];
    }

    let mut lines = Vec::new();
    let mut current = String::new();
    for word in text.split_whitespace() {
        let mut remaining = word.to_string();
        while display_len(&remaining) > width {
            let part = truncate_chars(&remaining, width);
            let split_at = part.len();
            lines.push(part);
            remaining = remaining[split_at..].to_string();
        }

        let word_len = display_len(&remaining);
        let current_len = display_len(&current);
        if current.is_empty() {
            current = remaining;
        } else if current_len + 1 + word_len <= width {
            current.push(' ');
            current.push_str(&remaining);
        } else {
            lines.push(current);
            current = remaining;
        }
    }

    if !current.is_empty() {
        lines.push(current);
    }
    if lines.is_empty() {
        lines.push(String::new());
    }
    lines
}

fn truncate_chars(text: &str, max_chars: usize) -> String {
    text.chars().take(max_chars).collect()
}

fn display_len(text: &str) -> usize {
    text.chars().count()
}

fn pad_right(text: &str, width: usize) -> String {
    let len = display_len(text);
    if len >= width {
        text.to_string()
    } else {
        format!("{}{}", text, " ".repeat(width - len))
    }
}

fn pad_left(text: &str, width: usize) -> String {
    let len = display_len(text);
    if len >= width {
        text.to_string()
    } else {
        format!("{}{}", " ".repeat(width - len), text)
    }
}

fn group_integer(value: u64) -> String {
    let digits = value.to_string();
    let mut out = String::new();
    for (index, ch) in digits.chars().enumerate() {
        if index > 0 && (digits.len() - index) % 3 == 0 {
            out.push(',');
        }
        out.push(ch);
    }
    out
}

pub(crate) fn ascii_fallback(text: &str) -> String {
    text.chars()
        .map(|ch| match ch {
            '‘' | '’' => '\'',
            '“' | '”' => '"',
            '–' | '—' => '-',
            '…' => '.',
            '°' => 'o',
            '€' => 'E',
            '£' => 'L',
            '¥' => 'Y',
            'á' | 'à' | 'â' | 'ä' | 'ã' | 'å' => 'a',
            'Á' | 'À' | 'Â' | 'Ä' | 'Ã' | 'Å' => 'A',
            'é' | 'è' | 'ê' | 'ë' => 'e',
            'É' | 'È' | 'Ê' | 'Ë' => 'E',
            'í' | 'ì' | 'î' | 'ï' => 'i',
            'Í' | 'Ì' | 'Î' | 'Ï' => 'I',
            'ó' | 'ò' | 'ô' | 'ö' | 'õ' => 'o',
            'Ó' | 'Ò' | 'Ô' | 'Ö' | 'Õ' => 'O',
            'ú' | 'ù' | 'û' | 'ü' => 'u',
            'Ú' | 'Ù' | 'Û' | 'Ü' => 'U',
            'ñ' => 'n',
            'Ñ' => 'N',
            'ç' => 'c',
            'Ç' => 'C',
            c if c.is_ascii() && !c.is_control() => c,
            _ => '?',
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::models::{PrinterType, ReceiptLine};

    fn sample_payload() -> ReceiptPayload {
        ReceiptPayload {
            printer_type: PrinterType::Voucher,
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
    fn escpos_starts_with_init_codepage_and_ends_with_partial_cut() {
        let bytes = render_escpos(&sample_payload(), 48);
        assert_eq!(&bytes[..5], &[0x1b, b'@', 0x1b, b't', 0]);
        assert_eq!(&bytes[bytes.len() - 3..], &[0x1d, b'V', 1]);
    }

    #[test]
    fn wide_preview_aligns_numeric_columns_within_48_chars() {
        let preview = render_text_preview(&sample_payload(), 48);
        let item_line = preview
            .lines()
            .find(|line| line.contains("Product Sample A"))
            .unwrap();

        assert_eq!(item_line.chars().count(), 48);
        assert!(item_line.ends_with("      58,800"));
        assert!(item_line.contains("    5"));
        assert!(item_line.contains("11,760"));
    }

    #[test]
    fn narrow_preview_uses_two_line_layout_for_58mm_width() {
        let preview = render_text_preview(&sample_payload(), 32);
        assert!(preview.lines().any(|line| line.contains("Product Sample A")));
        assert!(preview.lines().any(|line| line.contains("5 x 11,760")));
        for line in preview.lines().filter(|line| !line.contains("[partial cut]")) {
            assert!(line.chars().count() <= 32, "line too wide: {line:?}");
        }
    }

    #[test]
    fn long_item_names_wrap_without_exceeding_width() {
        let mut payload = sample_payload();
        payload.lines[0].name = "A very long product name that must wrap across receipt lines".to_string();
        let preview = render_text_preview(&payload, 48);
        assert!(preview.contains("must wrap"));
        for line in preview.lines().filter(|line| !line.contains("[partial cut]")) {
            assert!(line.chars().count() <= 48, "line too wide: {line:?}");
        }
    }

    #[test]
    fn escpos_output_is_safe_ascii_when_input_contains_unicode() {
        let mut payload = sample_payload();
        payload.shop_name = "ဆိုင် — Café".to_string();
        let bytes = render_escpos(&payload, 48);
        assert!(bytes.iter().all(|byte| *byte < 128));
        let text = String::from_utf8_lossy(&bytes);
        assert!(text.contains("Cafe"));
        assert!(text.contains('?'));
    }

    #[test]
    fn total_is_bold_in_escpos_command_stream() {
        let bytes = render_escpos(&sample_payload(), 48);
        let needle = [0x1b, b'E', 1];
        assert!(bytes.windows(3).any(|window| window == needle));
    }

    #[test]
    fn formats_grouped_money_and_compact_quantities() {
        assert_eq!(format_money(58800.0), "58,800");
        assert_eq!(format_money(1234.5), "1,234.50");
        assert_eq!(format_money(-1200.0), "-1,200");
        assert_eq!(format_qty(5.0), "5");
        assert_eq!(format_qty(1.25), "1.25");
    }
}
