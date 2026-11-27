use reqwest::blocking::Client;
use reqwest::header::{HeaderMap, HeaderValue};
use serde_json::{json, Value};
use std::error::Error;
use std::fmt::{Display, Formatter};

#[derive(Debug)]
pub struct ErpError(String);

impl Display for ErpError {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(&self.0)
    }
}

impl Error for ErpError {}

type ErpResult<T> = Result<T, Box<dyn Error + Send + Sync>>;

pub struct ErpClient {
    base_url: String,
    client: Client,
}

#[derive(Clone, Debug, serde::Serialize, serde::Deserialize)]
pub struct SaleItem {
    pub item_code: String,
    pub qty: f64,
    pub uom: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub rate: Option<f64>,
}

impl ErpClient {
    pub fn login(base_url: &str, username: &str, password: &str) -> ErpResult<Self> {
        let client = Client::builder().cookie_store(true).build()?;
        let response = client
            .post(format!(
                "{}/api/method/login",
                base_url.trim_end_matches('/')
            ))
            .json(&json!({"usr": username, "pwd": password}))
            .send()?;
        let csrf = response
            .headers()
            .get_all(reqwest::header::SET_COOKIE)
            .iter()
            .filter_map(|value| value.to_str().ok())
            .find_map(|cookie| {
                cookie
                    .split(';')
                    .next()?
                    .strip_prefix("csrf_token=")
                    .map(str::to_owned)
            });
        if !response.status().is_success() {
            return Err(Box::new(ErpError(format!(
                "ERP login failed with HTTP {}",
                response.status()
            ))));
        }
        let base_url = base_url.trim_end_matches('/').to_owned();
        let mut headers = HeaderMap::new();
        if let Some(csrf) = csrf {
            headers.insert("X-Frappe-CSRF-Token", HeaderValue::from_str(&csrf)?);
        }
        let client = Client::builder()
            .cookie_store(true)
            .default_headers(headers)
            .build()?;
        // Re-login through the CSRF-aware client so its cookie jar is authoritative for calls.
        let response = client
            .post(format!("{base_url}/api/method/login"))
            .json(&json!({"usr": username, "pwd": password}))
            .send()?;
        if !response.status().is_success() {
            return Err(Box::new(ErpError(format!(
                "ERP login failed with HTTP {}",
                response.status()
            ))));
        }
        Ok(Self { base_url, client })
    }

    pub fn post_method(&self, endpoint: &str, payload: &Value) -> ErpResult<Value> {
        let response = self
            .client
            .post(format!("{}/api/method/{endpoint}", self.base_url))
            .json(payload)
            .send()?;
        let status = response.status();
        let body = response
            .json::<Value>()
            .unwrap_or_else(|_| json!({"error": "non-json ERP response"}));
        if !status.is_success() {
            return Err(Box::new(ErpError(compact_error(&body))));
        }
        Ok(body.get("message").cloned().unwrap_or(body))
    }

    pub fn sync_catalog(
        &self,
        warehouse: &str,
        price_list: &str,
        state: Option<&str>,
        customer: Option<&str>,
    ) -> ErpResult<Value> {
        self.post_method(
            "khitmee_ops.api.catalog.sync_catalog",
            &json!({
                "warehouse": warehouse,
                "price_list": price_list,
                "state": state.unwrap_or(""),
                "customer": customer.unwrap_or(""),
            }),
        )
    }

    pub fn print_receipt(endpoint: &str, token: &str, payload: &Value) -> ErpResult<Value> {
        let client = Client::builder()
            .timeout(std::time::Duration::from_millis(2500))
            .build()?;
        let mut request = client.post(endpoint).json(payload);
        if !token.is_empty() {
            request = request.header("X-Khitmee-Print-Token", token);
        }
        let response = request.send()?;
        if !response.status().is_success() {
            return Err(Box::new(ErpError(format!(
                "print companion returned HTTP {}",
                response.status()
            ))));
        }
        Ok(response.json().unwrap_or_else(|_| json!({})))
    }

    pub fn submit_sale(
        &self,
        client_voucher_id: &str,
        warehouse: &str,
        customer: &str,
        price_list: &str,
        items: &[SaleItem],
        payment_mode: Option<&str>,
        printer_type: &str,
    ) -> ErpResult<Value> {
        self.post_method(
            "khitmee_ops.api.vouchers.submit_sale",
            &json!({
                "client_voucher_id": client_voucher_id,
                "warehouse": warehouse,
                "customer": customer,
                "price_list": price_list,
                "items": items,
                "payment_mode": payment_mode,
                "printer_type": printer_type,
                "override_rate": false,
            }),
        )
    }

    pub fn submit_transfer(
        &self,
        client_voucher_id: &str,
        source_warehouse: &str,
        target_warehouse: &str,
        transfer_mode: &str,
        outgoing_stock_entry: Option<&str>,
        items: &[SaleItem],
        printer_type: &str,
    ) -> ErpResult<Value> {
        self.post_method(
            "khitmee_ops.api.vouchers.submit_transfer",
            &json!({
                "client_voucher_id": client_voucher_id,
                "source_warehouse": source_warehouse,
                "target_warehouse": target_warehouse,
                "transfer_mode": transfer_mode,
                "outgoing_stock_entry": outgoing_stock_entry,
                "items": items,
                "printer_type": printer_type,
            }),
        )
    }
}

fn compact_error(body: &Value) -> String {
    if let Some(message) = body.get("_error_message").and_then(|value| value.as_str()) {
        return message.to_owned();
    }
    body.to_string().chars().take(300).collect()
}
