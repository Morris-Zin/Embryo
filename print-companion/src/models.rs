use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum PrinterType {
    Voucher,
    A4,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ReceiptLine {
    pub name: String,
    pub qty: f64,
    pub rate: f64,
    pub amount: f64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ReceiptPayload {
    pub printer_type: PrinterType,
    pub voucher_no: String,
    pub title: String,
    pub shop_name: String,
    pub date: String,
    pub party: String,
    pub lines: Vec<ReceiptLine>,
    pub total: f64,
    pub footer: String,
}

impl ReceiptPayload {
    pub fn validate(&self) -> Result<(), String> {
        if self.voucher_no.trim().is_empty() {
            return Err("voucher_no must not be empty".to_string());
        }
        if self.lines.is_empty() {
            return Err("lines must contain at least one item".to_string());
        }
        if self
            .lines
            .iter()
            .any(|line| !line.qty.is_finite() || !line.rate.is_finite() || !line.amount.is_finite())
            || !self.total.is_finite()
        {
            return Err("numeric receipt values must be finite".to_string());
        }
        Ok(())
    }
}

#[derive(Debug, Serialize)]
pub struct HealthResponse {
    pub status: &'static str,
    pub version: &'static str,
    pub dry_run: bool,
    pub voucher_printer: String,
    pub a4_printer: String,
}

#[derive(Debug, Serialize)]
pub struct PrintersResponse {
    pub printers: Vec<String>,
}

#[derive(Debug, Serialize)]
pub struct PrintResponse {
    pub status: &'static str,
    pub voucher_no: String,
    pub printer_type: PrinterType,
    pub dry_run: bool,
    pub artifacts: Vec<String>,
}

#[derive(Debug, Serialize)]
pub struct ErrorResponse {
    pub error: String,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn deserializes_contract_payload() {
        let json = r##"{
            "printer_type": "voucher",
            "voucher_no": "SINV-0001",
            "title": "Khit Me Sales Voucher",
            "shop_name": "Dist Hub 1",
            "date": "2026-11-26 16:00:00",
            "party": "Walk-in Customer",
            "lines": [
                {"name": "Product Sample A", "qty": 5, "rate": 11760, "amount": 58800}
            ],
            "total": 58800,
            "footer": "Thank you!"
        }"##;

        let payload: ReceiptPayload = serde_json::from_str(json).unwrap();

        assert_eq!(payload.printer_type, PrinterType::Voucher);
        assert_eq!(payload.voucher_no, "SINV-0001");
        assert_eq!(payload.lines.len(), 1);
        assert_eq!(payload.lines[0].qty, 5.0);
        assert_eq!(payload.total, 58800.0);
        payload.validate().unwrap();
    }

    #[test]
    fn deserializes_a4_printer_type() {
        let printer_type: PrinterType = serde_json::from_str(r##""a4""##).unwrap();
        assert_eq!(printer_type, PrinterType::A4);
    }

    #[test]
    fn rejects_empty_line_items() {
        let json = r##"{
            "printer_type": "voucher",
            "voucher_no": "SINV-0001",
            "title": "Khit Me Sales Voucher",
            "shop_name": "Dist Hub 1",
            "date": "2026-11-26 16:00:00",
            "party": "Walk-in Customer",
            "lines": [],
            "total": 0,
            "footer": "Thank you!"
        }"##;
        let payload: ReceiptPayload = serde_json::from_str(json).unwrap();
        assert!(payload.validate().is_err());
    }
}
