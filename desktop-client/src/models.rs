use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct CatalogItem {
    pub item_code: String,
    #[serde(default)]
    pub item_name: Option<String>,
    #[serde(default)]
    pub stock_uom: Option<String>,
    #[serde(default)]
    pub item_group: Option<String>,
    #[serde(default)]
    pub brand: Option<String>,
    #[serde(default)]
    pub barcode: Option<String>,
    #[serde(default)]
    pub barcode_text: Option<String>,
    #[serde(default)]
    pub rate: f64,
    #[serde(default)]
    pub actual_qty: f64,
    #[serde(default)]
    pub modified: Option<String>,
    #[serde(default)]
    pub deleted: bool,
}

#[derive(Debug, Deserialize)]
pub struct SyncPayload {
    pub scope: String,
    #[serde(default)]
    pub reset: bool,
    #[serde(default)]
    pub sync_token: String,
    #[serde(default)]
    pub items: Vec<CatalogItem>,
}

#[derive(Debug, Serialize)]
pub struct ErrorResponse {
    pub error: String,
}

#[derive(Debug, Serialize)]
pub struct HealthResponse<'a> {
    pub status: &'a str,
    pub version: &'a str,
    pub encrypted: bool,
    pub catalogs: usize,
    pub items: usize,
}

#[derive(Debug, Serialize)]
pub struct StateResponse {
    pub sync_token: String,
    pub item_count: usize,
}

#[derive(Debug, Serialize)]
pub struct SyncResponse {
    pub status: &'static str,
    pub item_count: usize,
}

#[derive(Debug, Serialize)]
pub struct SearchResponse {
    pub items: Vec<CatalogItem>,
}
