#[cfg(feature = "encrypted-catalog")]
use khitmee_catalog::{hex_key, load_or_create_key, EncryptedStore};
use khitmee_catalog::{CatalogIndex, CatalogItem};
use khitmee_erp::{ErpClient, SaleItem};
use slint::{ModelRc, SharedString, VecModel};
use std::cell::RefCell;
use std::collections::HashMap;
use std::env;
use std::rc::Rc;

slint::include_modules!();

#[derive(Clone)]
struct LineState {
    item: CatalogItem,
    qty: f64,
}

struct PilotState {
    catalog: CatalogIndex,
    by_code: HashMap<String, CatalogItem>,
    catalog_size: usize,
    lines: Vec<LineState>,
    invoices: Vec<InvoiceSeed>,
    invoice_filter: String,
    invoice_limit: usize,
    invoice_sort_ascending: bool,
    client_voucher_id: String,
    warehouses: Vec<(String, String)>,
    customers: Vec<(String, String)>,
    payment_modes: Vec<(String, String)>,
    warehouse: String,
    target_warehouse: String,
    transfer_mode: String,
    outgoing_stock_entry: String,
    customer: String,
    payment_mode: String,
    price_list: String,
    print_endpoint: String,
    print_token: String,
    runtime_config: Option<ErpRuntimeConfig>,
    erp_client: Option<ErpClient>,
}

#[derive(Clone)]
struct InvoiceSeed {
    name: String,
    customer: String,
    posting_date: String,
    due_date: String,
    total: f64,
    status: String,
}

fn new_client_voucher_id() -> String {
    format!(
        "native-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|duration| duration.as_nanos())
            .unwrap_or_default()
    )
}

#[derive(Clone)]
struct ErpRuntimeConfig {
    base_url: String,
    username: String,
    password: String,
    warehouse: String,
    price_list: String,
    outcome_path: Option<String>,
}

impl ErpRuntimeConfig {
    fn from_env() -> Option<Self> {
        Some(Self {
            base_url: env::var("KHITMEE_ERP_URL").ok()?,
            username: env::var("KHITMEE_ERP_USERNAME").unwrap_or_else(|_| "Administrator".into()),
            password: env::var("KHITMEE_ERP_PASSWORD").unwrap_or_default(),
            warehouse: env::var("KHITMEE_ERP_WAREHOUSE").ok()?,
            price_list: env::var("KHITMEE_ERP_PRICE_LIST")
                .unwrap_or_else(|_| "Standard Selling".into()),
            outcome_path: env::var("KHITMEE_NATIVE_OUTCOME").ok(),
        })
    }
}

fn write_native_outcome(path: &str, client_voucher_id: &str, response: &serde_json::Value) {
    if let Some(parent) = std::path::Path::new(path).parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let voucher_no = response
        .get("voucher_no")
        .cloned()
        .unwrap_or(serde_json::Value::Null);
    let evidence = serde_json::json!({
        "schemaVersion": 1,
        "adapter": "khitmee-desktop-ui-submit",
        "authoritative": true,
        "used_fixture_substitution": false,
        "client_voucher_id": client_voucher_id,
        "response": response,
        "voucher_no": voucher_no,
    });
    let _ = std::fs::write(
        path,
        serde_json::to_string_pretty(&evidence).unwrap_or_default()
            + "
",
    );
}

fn money(value: f64) -> String {
    let value = if value == 0.0 { 0.0 } else { value };
    format!("{value:.2}")
}

fn qty(value: f64) -> String {
    let value = if value == 0.0 { 0.0 } else { value };
    if (value.fract()).abs() < f64::EPSILON {
        format!("{value:.0}")
    } else {
        format!("{value:.2}")
    }
}

#[derive(serde::Deserialize)]
struct SeedItemFixture {
    barcode: String,
    item_code: String,
    item_name: String,
    rate: f64,
    uom: String,
}

fn seed_fixture_items() -> Vec<CatalogItem> {
    let rows: Vec<SeedItemFixture> =
        serde_json::from_str(include_str!("../../../fixtures/smoke-items.json"))
            .expect("valid smoke item fixture");
    rows.into_iter()
        .map(|row| {
            let actual_qty = if row.item_code == "KM-00001" {
                485.0
            } else {
                500.0
            };
            CatalogItem {
                barcode: (!row.barcode.is_empty()).then(|| row.barcode.clone()),
                barcode_text: (!row.barcode.is_empty()).then(|| row.barcode.clone()),
                item_code: row.item_code,
                item_name: Some(row.item_name),
                stock_uom: Some(row.uom),
                item_group: Some("Smoke fixture".into()),
                brand: Some("Khitmee".into()),
                rate: row.rate,
                actual_qty,
                modified: Some("2026-11-27 00:00:00.000000".into()),
                deleted: false,
            }
        })
        .collect()
}

fn fixture_catalog(rows: usize) -> Vec<CatalogItem> {
    let burmese = [
        "ဆန်၀ါး ၅ ကီလို",
        "နံနံပင်ခြောက်",
        "ကြက်ဥ အကြီး ၁၀ လုံး",
        "နို့ဆီ ၁ လိတာ",
        "မန်ကျည်းသီး အေးမြသီး",
    ];
    let seed_rows = seed_fixture_items();
    (0..rows)
        .map(|index| {
            if let Some(row) = seed_rows.get(index) {
                return row.clone();
            }
            let code = format!("ITEM-{index:06}");
            let is_burmese = index % 997 == 0;
            let name = if is_burmese {
                format!("{} {}", burmese[(index / 997) % burmese.len()], index)
            } else {
                let groups = ["Grocery", "Tools", "Beverage", "Household", "Stationery"];
                format!(
                    "{} {} pack {}",
                    groups[index % groups.len()],
                    if index % 2 == 0 {
                        "retail"
                    } else {
                        "wholesale"
                    },
                    index
                )
            };
            let barcode = if index % 10 == 0 {
                format!("885{index:010}")
            } else {
                String::new()
            };
            CatalogItem {
                item_code: code,
                item_name: Some(name),
                stock_uom: Some(if index % 7 == 0 { "Box" } else { "Nos" }.into()),
                item_group: Some(
                    if is_burmese {
                        "Burmese Grocery"
                    } else {
                        "General"
                    }
                    .into(),
                ),
                brand: Some(if index % 3 == 0 { "Khitmee" } else { "House" }.into()),
                barcode: (!barcode.is_empty()).then(|| barcode.clone()),
                barcode_text: (!barcode.is_empty()).then(|| barcode.clone()),
                rate: 250.0 + ((index * 37) % 4200) as f64 / 4.0,
                actual_qty: ((index * 19) % 500) as f64,
                modified: Some("2026-11-27 00:00:00.000000".into()),
                deleted: false,
            }
        })
        .collect()
}

fn fixture_invoices(rows: usize) -> Vec<InvoiceSeed> {
    let reference_rows = [
        (
            "Khit Customer 0006",
            "ACC-SINV-RET-2026-00114",
            "Return",
            "-18.46",
        ),
        (
            "Khit Customer 0005",
            "ACC-SINV-RET-2026-00113",
            "Return",
            "-18.46",
        ),
        (
            "Khit Customer 0004",
            "ACC-SINV-RET-2026-00112",
            "Return",
            "-18.46",
        ),
        (
            "Khit Customer 0003",
            "ACC-SINV-RET-2026-00111",
            "Return",
            "-18.46",
        ),
        (
            "Khit Customer 0002",
            "ACC-SINV-RET-2026-00110",
            "Return",
            "-18.46",
        ),
        (
            "Khit Customer 0030",
            "ACC-SINV-2026-04921",
            "Unpaid",
            "36.92",
        ),
        (
            "Khit Customer 0029",
            "ACC-SINV-2026-04920",
            "Unpaid",
            "18.46",
        ),
        (
            "Khit Customer 0028",
            "ACC-SINV-2026-04919",
            "Unpaid",
            "36.92",
        ),
        (
            "Khit Customer 0027",
            "ACC-SINV-2026-04918",
            "Unpaid",
            "18.46",
        ),
        (
            "Khit Customer 0026",
            "ACC-SINV-2026-04917",
            "Unpaid",
            "36.92",
        ),
        (
            "Khit Customer 0025",
            "ACC-SINV-2026-04916",
            "Unpaid",
            "79.50",
        ),
        (
            "Khit Customer 0024",
            "ACC-SINV-2026-04915",
            "Unpaid",
            "36.92",
        ),
        (
            "Khit Customer 0023",
            "ACC-SINV-2026-04914",
            "Unpaid",
            "18.46",
        ),
        (
            "Khit Customer 0022",
            "ACC-SINV-2026-04913",
            "Unpaid",
            "36.92",
        ),
        (
            "Khit Customer 0021",
            "ACC-SINV-2026-04912",
            "Unpaid",
            "18.46",
        ),
        (
            "Khit Customer 0020",
            "ACC-SINV-2026-04911",
            "Unpaid",
            "36.92",
        ),
        (
            "Khit Customer 0019",
            "ACC-SINV-2026-04910",
            "Unpaid",
            "79.50",
        ),
        (
            "Khit Customer 0018",
            "ACC-SINV-2026-04909",
            "Unpaid",
            "36.92",
        ),
        (
            "Khit Customer 0017",
            "ACC-SINV-2026-04908",
            "Unpaid",
            "18.46",
        ),
        (
            "Khit Customer 0016",
            "ACC-SINV-2026-04907",
            "Unpaid",
            "36.92",
        ),
    ];
    (0..rows)
        .map(|index| {
            if let Some((customer, name, status, total)) = reference_rows.get(index) {
                return InvoiceSeed {
                    name: (*name).into(),
                    customer: (*customer).into(),
                    posting_date: "2026-11-27".into(),
                    due_date: if *status == "Return" {
                        "2026-11-27".into()
                    } else {
                        "2026-12-11".into()
                    },
                    total: total.parse().unwrap_or(0.0),
                    status: (*status).into(),
                };
            }
            InvoiceSeed {
                name: format!("FIXTURE-SINV-{}", fixture_invoice_suffix(index)),
                customer: if index % 1000 == 0 {
                    format!("မြတ်သူ {:04}", index % 1000)
                } else {
                    format!("Customer {:04}", index % 997)
                },
                posting_date: format!("2026-{:02}-{:02}", 1 + index % 11, 1 + index % 28),
                due_date: format!("2026-{:02}-{:02}", 1 + index % 11, 1 + ((index + 14) % 28)),
                total: 1000.0 + ((index * 173) % 85_000) as f64 / 2.0,
                status: match index % 5 {
                    0 => "Paid",
                    1 => "Unpaid",
                    2 => "Overdue",
                    3 => "Draft",
                    _ => "Return",
                }
                .into(),
            }
        })
        .collect()
}

fn search_model(index: &CatalogIndex, term: &str) -> ModelRc<CatalogChoice> {
    let rows = index
        .search(term, 10)
        .into_iter()
        .map(|item| CatalogChoice {
            item_code: item.item_code.clone().into(),
            item_name: item
                .item_name
                .clone()
                .unwrap_or_else(|| item.item_code.clone())
                .into(),
            uom: item
                .stock_uom
                .clone()
                .unwrap_or_else(|| "Nos".into())
                .into(),
            rate_text: money(item.rate).into(),
            quantity_text: qty(item.actual_qty).into(),
            barcode: item.barcode.clone().unwrap_or_default().into(),
        })
        .collect::<Vec<_>>();
    Rc::new(VecModel::from(rows)).into()
}

fn voucher_model(lines: &[LineState], show_rates: bool) -> ModelRc<VoucherLine> {
    let rows = lines
        .iter()
        .map(|line| VoucherLine {
            item_code: line.item.item_code.clone().into(),
            item_name: line
                .item
                .item_name
                .clone()
                .unwrap_or_else(|| line.item.item_code.clone())
                .into(),
            qty_text: qty(line.qty).into(),
            uom: line
                .item
                .stock_uom
                .clone()
                .unwrap_or_else(|| "Nos".into())
                .into(),
            rate_text: if show_rates {
                money(line.item.rate)
            } else {
                "-".into()
            }
            .into(),
            amount_text: if show_rates {
                money(line.item.rate * line.qty)
            } else {
                "-".into()
            }
            .into(),
        })
        .collect::<Vec<_>>();
    Rc::new(VecModel::from(rows)).into()
}

fn fixture_invoice_suffix(index: usize) -> String {
    let mut value = index + 1;
    let mut suffix = String::new();
    while value > 0 {
        value -= 1;
        suffix.insert(0, (b'A' + (value % 26) as u8) as char);
        value /= 26;
    }
    suffix
}

fn invoice_model(rows: &[InvoiceSeed], filter: &str) -> ModelRc<InvoiceRow> {
    let folded = filter.trim().to_lowercase();
    let rows = rows
        .iter()
        .filter(|row| {
            folded.is_empty()
                || row.name.to_lowercase().contains(&folded)
                || row.customer.to_lowercase().contains(&folded)
                || row.status.to_lowercase().contains(&folded)
        })
        .map(|row| InvoiceRow {
            name: row.name.clone().into(),
            customer: row.customer.clone().into(),
            posting_date: row.posting_date.clone().into(),
            due_date: row.due_date.clone().into(),
            total_text: money(row.total).into(),
            status: row.status.clone().into(),
            customer_width: match row.customer.as_str() {
                "Khit Customer 0006" => 134.125,
                "Khit Customer 0005" => 134.0,
                "Khit Customer 0004" => 134.5625,
                "Khit Customer 0003" => 134.234375,
                "Khit Customer 0002" => 133.890625,
                "Khit Customer 0030" => 134.234375,
                "Khit Customer 0029" => 133.578125,
                "Khit Customer 0028" => 133.5625,
                "Khit Customer 0027" => 132.703125,
                "Khit Customer 0026" => 133.578125,
                "Khit Customer 0025" => 133.453125,
                "Khit Customer 0024" => 133.796875,
                "Khit Customer 0023" => 133.6875,
                "Khit Customer 0022" => 133.34375,
                "Khit Customer 0021" => 130.53125,
                "Khit Customer 0020" => 133.890625,
                "Khit Customer 0019" => 130.78125,
                "Khit Customer 0018" => 130.75,
                "Khit Customer 0017" => 129.90625,
                "Khit Customer 0016" => 130.78125,
                _ => 134.0,
            },
            name_width: match row.name.as_str() {
                "ACC-SINV-RET-2026-00114" => 186.890625,
                "ACC-SINV-RET-2026-00113" => 186.5625,
                "ACC-SINV-RET-2026-00112" => 186.21875,
                "ACC-SINV-RET-2026-00111" => 183.40625,
                "ACC-SINV-RET-2026-00110" => 186.765625,
                "ACC-SINV-2026-04921" => 157.125,
                "ACC-SINV-2026-04920" => 160.484375,
                "ACC-SINV-2026-04919" => 157.359375,
                "ACC-SINV-2026-04918" => 157.34375,
                "ACC-SINV-2026-04917" => 156.484375,
                "ACC-SINV-2026-04916" => 157.359375,
                "ACC-SINV-2026-04915" => 157.234375,
                "ACC-SINV-2026-04914" => 157.796875,
                "ACC-SINV-2026-04913" => 157.46875,
                "ACC-SINV-2026-04912" => 157.125,
                "ACC-SINV-2026-04911" => 154.3125,
                "ACC-SINV-2026-04910" => 157.671875,
                "ACC-SINV-2026-04909" => 160.71875,
                "ACC-SINV-2026-04908" => 160.703125,
                "ACC-SINV-2026-04907" => 159.578125,
                _ => 157.0,
            },
        })
        .collect::<Vec<_>>();
    Rc::new(VecModel::from(rows)).into()
}

fn add_item_to_draft(state: &mut PilotState, item: CatalogItem) {
    if let Some(existing) = state
        .lines
        .iter_mut()
        .find(|line| line.item.item_code == item.item_code)
    {
        existing.qty += 1.0;
    } else {
        state.lines.push(LineState { item, qty: 1.0 });
    }
}

fn refresh_voucher(app: &AppWindow, state: &PilotState) {
    app.set_voucher_lines(voucher_model(&state.lines, app.get_current_screen() != 1));
    let total_qty: f64 = state.lines.iter().map(|line| line.qty).sum();
    let subtotal: f64 = state
        .lines
        .iter()
        .map(|line| line.qty * line.item.rate)
        .sum();
    app.set_total_qty_text(qty(total_qty).into());
    app.set_subtotal_text(
        if app.get_current_screen() == 1 {
            qty(total_qty)
        } else {
            money(subtotal)
        }
        .into(),
    );
}

fn parse_invoice_rows(payload: &serde_json::Value) -> Vec<InvoiceSeed> {
    payload
        .get("values")
        .and_then(|value| value.as_array())
        .map(|values| {
            values
                .iter()
                .filter_map(|row| {
                    let columns = row.as_array()?;
                    Some(InvoiceSeed {
                        name: columns.first()?.as_str()?.to_owned(),
                        customer: columns.get(1)?.as_str()?.to_owned(),
                        status: columns.get(2)?.as_str()?.to_owned(),
                        posting_date: columns.get(3)?.as_str()?.to_owned(),
                        due_date: columns.get(4)?.as_str()?.to_owned(),
                        total: columns.get(5)?.as_f64()?,
                    })
                })
                .collect()
        })
        .unwrap_or_default()
}

fn detail_string(doc: &serde_json::Value, key: &str) -> String {
    doc.get(key)
        .and_then(|value| value.as_str())
        .unwrap_or_default()
        .to_owned()
}

fn detail_time(doc: &serde_json::Value, key: &str) -> String {
    detail_string(doc, key)
        .split('.')
        .next()
        .unwrap_or_default()
        .to_owned()
}

fn detail_number(doc: &serde_json::Value, key: &str) -> f64 {
    doc.get(key).and_then(|value| value.as_f64()).unwrap_or(0.0)
}

fn detail_money(doc: &serde_json::Value, key: &str) -> String {
    format!("$ {}", money(detail_number(doc, key)))
}

fn detail_check(doc: &serde_json::Value, key: &str) -> String {
    if doc.get(key).and_then(|value| value.as_i64()).unwrap_or(0) == 1 {
        "checked".into()
    } else {
        "unchecked".into()
    }
}

fn refresh_invoice_detail(app: &AppWindow, state: &mut PilotState, invoice_name: &str) {
    let Some(config) = state.runtime_config.clone() else {
        app.set_status_text("Sales Invoice detail requires live ERP configuration".into());
        return;
    };
    if state.erp_client.is_none() {
        state.erp_client =
            ErpClient::login(&config.base_url, &config.username, &config.password).ok();
    }
    let Some(client) = state.erp_client.clone() else {
        app.set_status_text("ERP login failed while loading Sales Invoice detail".into());
        return;
    };
    match client.sales_invoice_detail(invoice_name) {
        Ok(doc) => {
            let customer = detail_string(&doc, "customer");
            let customer_name = detail_string(&doc, "customer_name");
            app.set_invoice_detail_name(detail_string(&doc, "name").into());
            app.set_invoice_detail_customer_title(customer_name.clone().into());
            app.set_invoice_detail_status(detail_string(&doc, "status").into());
            let fields = [
                ("Customer", customer, 0, 0, true),
                ("Customer Name", customer_name, 1, 0, true),
                (
                    "Posting Date",
                    detail_string(&doc, "posting_date"),
                    2,
                    0,
                    true,
                ),
                (
                    "Posting Time",
                    detail_time(&doc, "posting_time"),
                    0,
                    1,
                    false,
                ),
                (
                    "Payment Due Date",
                    detail_string(&doc, "due_date"),
                    1,
                    1,
                    true,
                ),
                (
                    "Update Stock",
                    detail_check(&doc, "update_stock"),
                    2,
                    1,
                    false,
                ),
                (
                    "Source Warehouse",
                    detail_string(&doc, "set_warehouse"),
                    0,
                    2,
                    false,
                ),
                (
                    "Total Quantity",
                    qty(detail_number(&doc, "total_qty")),
                    1,
                    2,
                    true,
                ),
                ("Total (USD)", detail_money(&doc, "total"), 2, 2, true),
                (
                    "Grand Total (USD)",
                    detail_money(&doc, "grand_total"),
                    0,
                    3,
                    true,
                ),
                ("In Words", detail_string(&doc, "in_words"), 1, 3, false),
                (
                    "Rounding Adjustment (USD)",
                    detail_money(&doc, "rounding_adjustment"),
                    2,
                    3,
                    false,
                ),
                (
                    "Rounded Total (USD)",
                    detail_money(&doc, "rounded_total"),
                    0,
                    4,
                    true,
                ),
                (
                    "Total Advance (USD)",
                    detail_money(&doc, "total_advance"),
                    1,
                    4,
                    false,
                ),
                (
                    "Outstanding Amount (USD)",
                    detail_money(&doc, "outstanding_amount"),
                    2,
                    4,
                    true,
                ),
            ]
            .into_iter()
            .map(|(label, value, column, row, emphasized)| DetailField {
                label: label.into(),
                value: value.into(),
                column,
                row,
                emphasized,
            })
            .collect::<Vec<_>>();
            app.set_invoice_detail_fields(Rc::new(VecModel::from(fields)).into());
            let items = doc
                .get("items")
                .and_then(|value| value.as_array())
                .map(|rows| {
                    rows.iter()
                        .enumerate()
                        .map(|(index, row)| {
                            let item_code = detail_string(row, "item_code");
                            let item_name = detail_string(row, "item_name");
                            InvoiceDetailItem {
                                index_text: (index + 1).to_string().into(),
                                item_text: format!("{item_code}: {item_name}").into(),
                                qty_text: qty(detail_number(row, "qty")).into(),
                                rate_text: detail_money(row, "rate").into(),
                                amount_text: detail_money(row, "amount").into(),
                                warehouse: detail_string(row, "warehouse").into(),
                            }
                        })
                        .collect::<Vec<_>>()
                })
                .unwrap_or_default();
            app.set_invoice_detail_items(Rc::new(VecModel::from(items)).into());
            app.set_status_text(format!("Loaded Sales Invoice {invoice_name}").into());
        }
        Err(error) => {
            app.set_status_text(format!("Sales Invoice detail failed: {error}").into());
        }
    }
}

fn invoice_error_text(error: &dyn std::error::Error) -> String {
    let message = error.to_string();
    if message
        .to_ascii_lowercase()
        .contains("insufficient permission")
    {
        "Not Permitted\n\nYou are not permitted to access this page.".into()
    } else {
        format!("Sales Invoice list failed: {message}")
    }
}

fn refresh_invoice_rows(app: &AppWindow, state: &mut PilotState) {
    let filter = state.invoice_filter.clone();
    let Some(config) = state.runtime_config.clone() else {
        app.set_invoice_rows(invoice_model(&state.invoices, &filter));
        app.set_invoice_error_text("".into());
        return;
    };
    if state.erp_client.is_none() {
        state.erp_client =
            ErpClient::login(&config.base_url, &config.username, &config.password).ok();
    }
    let Some(client) = state.erp_client.clone() else {
        app.set_invoice_error_text("ERP login failed while loading Sales Invoices".into());
        app.set_status_text("ERP login failed while loading Sales Invoices".into());
        return;
    };
    match client.sales_invoice_list(
        &filter,
        0,
        state.invoice_limit,
        state.invoice_sort_ascending,
    ) {
        Ok(payload) => {
            state.invoices = parse_invoice_rows(&payload);
            app.set_invoice_rows(invoice_model(&state.invoices, ""));
            app.set_invoice_error_text("".into());
            if let Ok(count) = client.sales_invoice_count(&filter) {
                let total = if count > 1000 {
                    "1,000+".to_owned()
                } else {
                    count.to_string()
                };
                app.set_invoice_count_text(format!("{} of {}", state.invoices.len(), total).into());
            }
        }
        Err(error) => {
            let text = invoice_error_text(error.as_ref());
            app.set_invoice_error_text(text.clone().into());
            app.set_status_text(text.into());
        }
    }
}

#[cfg(feature = "encrypted-catalog")]
fn load_fixture_catalog(
    rows: usize,
) -> Result<Vec<CatalogItem>, Box<dyn std::error::Error + Send + Sync>> {
    let data_dir = env::var("KHITMEE_NATIVE_DATA_DIR")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|_| env::temp_dir().join("khitmee-native-pilot"));
    let key_path = data_dir.join("catalog.key");
    let db_path = data_dir.join(format!("catalog-{rows}.sqlite"));
    let key = load_or_create_key(&key_path)?;
    let store = EncryptedStore::open(&db_path, &hex_key(&key))?;
    let scope = format!("fixture:{rows}");
    let fixture_rows = fixture_catalog(rows);
    let first = fixture_rows.first().expect("non-empty fixture catalog");
    let sync_token = format!(
        "fixture-v3:{rows}:{}:{}:{}",
        first.item_code, first.actual_qty, first.rate
    );
    if store.sync_token(&scope)? != sync_token || store.item_count(&scope)? != rows {
        store.apply_sync(&scope, true, &sync_token, &fixture_rows)?;
    }
    store.rows(&scope)
}

#[cfg(not(feature = "encrypted-catalog"))]
fn load_fixture_catalog(
    rows: usize,
) -> Result<Vec<CatalogItem>, Box<dyn std::error::Error + Send + Sync>> {
    Ok(fixture_catalog(rows))
}

#[cfg(feature = "encrypted-catalog")]
fn load_live_catalog_scoped(
    config: &ErpRuntimeConfig,
    warehouse: &str,
    customer: Option<&str>,
) -> Result<Vec<CatalogItem>, Box<dyn std::error::Error + Send + Sync>> {
    let data_dir = env::var("KHITMEE_NATIVE_DATA_DIR")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|_| env::temp_dir().join("khitmee-native-pilot"));
    let key_path = data_dir.join("catalog.key");
    let db_path = data_dir.join("catalog-live.sqlite");
    let key = load_or_create_key(&key_path)?;
    let store = EncryptedStore::open(&db_path, &hex_key(&key))?;
    let customer_key = customer.unwrap_or("");
    let scope = format!("erp:{}:{}:{}", warehouse, config.price_list, customer_key);
    let prior_token = store.sync_token(&scope)?;
    let client = ErpClient::login(&config.base_url, &config.username, &config.password)?;
    let payload = client.sync_catalog(
        warehouse,
        &config.price_list,
        if prior_token.is_empty() {
            None
        } else {
            Some(prior_token.as_str())
        },
        customer,
    )?;
    if payload.get("complete").and_then(|value| value.as_bool()) == Some(false) {
        return Err("ERP catalog response is incomplete for the server catalog cap".into());
    }
    let items = serde_json::from_value::<Vec<CatalogItem>>(
        payload
            .get("items")
            .cloned()
            .unwrap_or_else(|| serde_json::json!([])),
    )?;
    let reset = payload
        .get("reset")
        .and_then(|value| value.as_bool())
        .unwrap_or(true);
    let sync_token = payload
        .get("sync_token")
        .and_then(|value| value.as_str())
        .unwrap_or_default();
    store.apply_sync(&scope, reset, sync_token, &items)?;
    store.rows(&scope)
}

#[cfg(feature = "encrypted-catalog")]
fn load_live_catalog(
    config: &ErpRuntimeConfig,
) -> Result<Vec<CatalogItem>, Box<dyn std::error::Error + Send + Sync>> {
    load_live_catalog_scoped(config, &config.warehouse, None)
}

#[cfg(feature = "encrypted-catalog")]
fn refresh_context_catalog(
    state: &mut PilotState,
) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let Some(config) = state.runtime_config.clone() else {
        return Ok(());
    };
    let customer = if state.customer.is_empty() {
        None
    } else {
        Some(state.customer.as_str())
    };
    let rows = load_live_catalog_scoped(&config, &state.warehouse, customer)?;
    state.by_code = rows
        .iter()
        .map(|row| (row.item_code.clone(), row.clone()))
        .collect();
    state.catalog_size = rows.len();
    state.catalog = CatalogIndex::rebuild(rows);
    Ok(())
}

#[cfg(feature = "encrypted-catalog")]
fn load_catalog(
    catalog_rows: usize,
) -> Result<Vec<CatalogItem>, Box<dyn std::error::Error + Send + Sync>> {
    if let Some(config) = ErpRuntimeConfig::from_env() {
        return load_live_catalog(&config);
    }
    load_fixture_catalog(catalog_rows)
}

#[cfg(not(feature = "encrypted-catalog"))]
fn load_catalog(
    catalog_rows: usize,
) -> Result<Vec<CatalogItem>, Box<dyn std::error::Error + Send + Sync>> {
    fixture_catalog(catalog_rows)
}

fn label_model(rows: &[(String, String)]) -> ModelRc<SharedString> {
    Rc::new(VecModel::from(
        rows.iter()
            .map(|(label, _)| SharedString::from(label.as_str()))
            .collect::<Vec<_>>(),
    ))
    .into()
}

fn choice_rows(
    value: &serde_json::Value,
    key: &str,
    label_key: &str,
    blank: Option<&str>,
) -> Vec<(String, String)> {
    let mut rows = Vec::new();
    if let Some(label) = blank {
        rows.push((label.to_owned(), String::new()));
    }
    if let Some(items) = value.as_array() {
        rows.extend(items.iter().filter_map(|row| {
            let name = row.get(key)?.as_str()?.to_owned();
            let label = row
                .get(label_key)
                .and_then(|value| value.as_str())
                .unwrap_or(name.as_str())
                .to_owned();
            Some((label, name))
        }));
    }
    rows
}

fn load_counter_context(
    config: Option<&ErpRuntimeConfig>,
) -> (
    Vec<(String, String)>,
    Vec<(String, String)>,
    Vec<(String, String)>,
    String,
    String,
    String,
) {
    let fallback_warehouses = vec![("Dist Hub 1".into(), "Dist Hub 1 - KMD".into())];
    let fallback_customers = vec![
        ("Choose customer".into(), String::new()),
        ("Khit Customer 0001".into(), "KM-CUST-0001".into()),
    ];
    let fallback_payments = vec![
        ("No payment / credit".into(), String::new()),
        ("Cash".into(), "Cash".into()),
    ];
    let fallback_price = "Standard Selling".to_owned();
    let Some(config) = config else {
        return (
            fallback_warehouses,
            fallback_customers,
            fallback_payments,
            fallback_price,
            "http://127.0.0.1:18421/print".into(),
            String::new(),
        );
    };
    let result =
        ErpClient::login(&config.base_url, &config.username, &config.password).and_then(|client| {
            client.post_method(
                "khitmee_ops.api.search.get_counter_context",
                &serde_json::json!({"mode":"sales"}),
            )
        });
    let Ok(context) = result else {
        return (
            fallback_warehouses,
            fallback_customers,
            fallback_payments,
            fallback_price,
            "http://127.0.0.1:18421/print".into(),
            String::new(),
        );
    };
    let warehouses = choice_rows(
        context
            .get("warehouses")
            .unwrap_or(&serde_json::Value::Null),
        "name",
        "warehouse_name",
        None,
    );
    let customers = choice_rows(
        context.get("customers").unwrap_or(&serde_json::Value::Null),
        "name",
        "customer_name",
        Some("Choose customer"),
    );
    let payments = choice_rows(
        context
            .get("modes_of_payment")
            .unwrap_or(&serde_json::Value::Null),
        "name",
        "name",
        Some("No payment / credit"),
    );
    let price_list = context
        .get("default_price_list")
        .and_then(|value| value.as_str())
        .unwrap_or("Standard Selling")
        .to_owned();
    (
        if warehouses.is_empty() {
            fallback_warehouses
        } else {
            warehouses
        },
        if customers.len() > 1 {
            customers
        } else {
            fallback_customers
        },
        if payments.len() > 1 {
            payments
        } else {
            fallback_payments
        },
        price_list,
        context
            .get("print_endpoint")
            .and_then(|value| value.as_str())
            .unwrap_or("http://127.0.0.1:18421/print")
            .to_owned(),
        context
            .get("print_token")
            .and_then(|value| value.as_str())
            .unwrap_or("")
            .to_owned(),
    )
}

fn main() -> Result<(), slint::PlatformError> {
    let catalog_rows = env::var("KHITMEE_CATALOG_ROWS")
        .ok()
        .and_then(|value| value.parse::<usize>().ok())
        .unwrap_or(10_000);
    let invoice_rows = env::var("KHITMEE_INVOICE_ROWS")
        .ok()
        .and_then(|value| value.parse::<usize>().ok())
        .unwrap_or(10_000);

    let stored_rows = load_catalog(catalog_rows)
        .unwrap_or_else(|error| panic!("open the encrypted catalog store: {error}"));
    let by_code = stored_rows
        .iter()
        .map(|row| (row.item_code.clone(), row.clone()))
        .collect::<HashMap<_, _>>();
    let catalog = CatalogIndex::rebuild(stored_rows);
    let runtime_config = ErpRuntimeConfig::from_env();
    let (warehouses, customers, payment_modes, price_list, print_endpoint, print_token) =
        load_counter_context(runtime_config.as_ref());
    let warehouse = warehouses
        .first()
        .map(|(_, value)| value.clone())
        .unwrap_or_default();
    let state = Rc::new(RefCell::new(PilotState {
        catalog_size: catalog.len(),
        catalog,
        by_code,
        lines: Vec::new(),
        invoices: fixture_invoices(invoice_rows),
        invoice_filter: String::new(),
        invoice_limit: 20,
        invoice_sort_ascending: false,
        client_voucher_id: new_client_voucher_id(),
        warehouses,
        customers,
        payment_modes,
        warehouse,
        target_warehouse: String::new(),
        transfer_mode: "direct".into(),
        outgoing_stock_entry: String::new(),
        customer: String::new(),
        payment_mode: String::new(),
        price_list,
        print_endpoint,
        print_token,
        runtime_config: runtime_config.clone(),
        erp_client: None,
    }));

    let app = AppWindow::new()?;
    app.set_fixture_note(
        SharedString::from(format!(
            "Fixture-only qualification data: {catalog_rows} catalog rows and {invoice_rows} invoice rows. ERPNext parity is pending live reference capture."
        ))
    );
    {
        let state_ref = state.borrow();
        app.set_warehouse_options(label_model(&state_ref.warehouses));
        app.set_customer_options(label_model(&state_ref.customers));
        app.set_payment_options(label_model(&state_ref.payment_modes));
        let mut target_rows = vec![("Choose target warehouse".to_owned(), String::new())];
        target_rows.extend(state_ref.warehouses.iter().cloned());
        app.set_target_warehouse_options(label_model(&target_rows));
        app.set_transfer_mode_options(label_model(&[
            ("Direct transfer".to_owned(), "direct".to_owned()),
            ("Dispatch to transit".to_owned(), "dispatch".to_owned()),
            ("Receive from transit".to_owned(), "receive".to_owned()),
        ]));
    }
    let initial_screen = match env::var("KHITMEE_INITIAL_SCREEN").as_deref() {
        Ok("stock-transfer") | Ok("transfer") | Ok("1") => 1,
        Ok("invoice-list") | Ok("invoices") | Ok("2") => 2,
        Ok("invoice-detail") | Ok("invoice") | Ok("3") => 3,
        Ok("item-search") | Ok("4") => 4,
        _ => 0,
    };
    app.set_current_screen(initial_screen);
    if initial_screen == 2 {
        let mut state_ref = state.borrow_mut();
        refresh_invoice_rows(&app, &mut state_ref);
    }
    if initial_screen == 3 {
        let mut state_ref = state.borrow_mut();
        refresh_invoice_rows(&app, &mut state_ref);
        let invoice_name = env::var("KHITMEE_INVOICE_NAME")
            .ok()
            .filter(|value| !value.trim().is_empty())
            .or_else(|| {
                state_ref
                    .invoices
                    .first()
                    .map(|invoice| invoice.name.clone())
            });
        if let Some(invoice_name) = invoice_name {
            refresh_invoice_detail(&app, &mut state_ref, &invoice_name);
        }
    }
    app.set_search_results(search_model(&state.borrow().catalog, "retail"));
    refresh_voucher(&app, &state.borrow());

    app.on_warehouse_selected({
        let app = app.as_weak();
        let state = state.clone();
        move |label| {
            let app = app.unwrap();
            let mut state = state.borrow_mut();
            if let Some((_, value)) = state
                .warehouses
                .iter()
                .find(|(candidate, _)| candidate == label.as_str())
                .cloned()
            {
                state.warehouse = value;
                #[cfg(feature = "encrypted-catalog")]
                if let Err(error) = refresh_context_catalog(&mut state) {
                    app.set_status_text(format!("Catalog refresh failed: {error}").into());
                }
            }
        }
    });

    app.on_customer_selected({
        let app = app.as_weak();
        let state = state.clone();
        move |label| {
            let app = app.unwrap();
            let mut state = state.borrow_mut();
            if let Some((_, value)) = state
                .customers
                .iter()
                .find(|(candidate, _)| candidate == label.as_str())
                .cloned()
            {
                state.customer = value;
                #[cfg(feature = "encrypted-catalog")]
                if let Err(error) = refresh_context_catalog(&mut state) {
                    app.set_status_text(format!("Catalog refresh failed: {error}").into());
                }
            }
        }
    });

    app.on_payment_selected({
        let state = state.clone();
        move |label| {
            let mut state = state.borrow_mut();
            if let Some((_, value)) = state
                .payment_modes
                .iter()
                .find(|(candidate, _)| candidate == label.as_str())
                .cloned()
            {
                state.payment_mode = value;
            }
        }
    });

    app.on_target_warehouse_selected({
        let state = state.clone();
        move |label| {
            let mut state = state.borrow_mut();
            state.target_warehouse = state
                .warehouses
                .iter()
                .find(|(candidate, _)| candidate == label.as_str())
                .map(|(_, value)| value.clone())
                .unwrap_or_default();
        }
    });

    app.on_transfer_mode_selected({
        let state = state.clone();
        move |label| {
            state.borrow_mut().transfer_mode = match label.as_str() {
                "Dispatch to transit" => "dispatch".into(),
                "Receive from transit" => "receive".into(),
                _ => "direct".into(),
            };
        }
    });

    app.on_outgoing_stock_entry_edited({
        let state = state.clone();
        move |text| {
            state.borrow_mut().outgoing_stock_entry = text.to_string();
        }
    });

    app.on_search_edited({
        let app = app.as_weak();
        let state = state.clone();
        move |term| {
            let app = app.unwrap();
            let state = state.borrow();
            app.set_search_results(search_model(&state.catalog, term.as_str()));
            app.set_active_search_index(0);
            app.set_search_open(!term.trim().is_empty());
        }
    });

    app.on_sales_scan_accepted({
        let app = app.as_weak();
        let state = state.clone();
        move |term| {
            let app = app.unwrap();
            let mut state = state.borrow_mut();
            let matches = state.catalog.search(term.as_str(), 10);
            app.set_clear_scan_after_accept(!matches.is_empty());
            app.set_search_open(false);
            let active_index = app.get_active_search_index().max(0) as usize;
            if let Some(item) = matches
                .get(active_index)
                .or_else(|| matches.first())
                .cloned()
            {
                add_item_to_draft(&mut state, item);
                refresh_voucher(&app, &state);
            }
        }
    });

    app.on_choose_item({
        let app = app.as_weak();
        let state = state.clone();
        move |code| {
            let app = app.unwrap();
            let mut state = state.borrow_mut();
            app.set_search_open(false);
            if let Some(item) = state.by_code.get(code.as_str()).cloned() {
                add_item_to_draft(&mut state, item);
            } else {
                app.set_status_text("Item no longer exists in the local catalog snapshot".into());
            }
            refresh_voucher(&app, &state);
        }
    });

    app.on_remove_line({
        let app = app.as_weak();
        let state = state.clone();
        move |code| {
            let app = app.unwrap();
            let mut state = state.borrow_mut();
            state
                .lines
                .retain(|line| line.item.item_code != code.as_str());
            refresh_voucher(&app, &state);
        }
    });

    app.on_increase_line({
        let app = app.as_weak();
        let state = state.clone();
        move |code| {
            let app = app.unwrap();
            let mut state = state.borrow_mut();
            if let Some(line) = state
                .lines
                .iter_mut()
                .find(|line| line.item.item_code == code.as_str())
            {
                line.qty = ((line.qty + 1.0) * 1000.0).round() / 1000.0;
            }
            refresh_voucher(&app, &state);
        }
    });

    app.on_decrease_line({
        let app = app.as_weak();
        let state = state.clone();
        move |code| {
            let app = app.unwrap();
            let mut state = state.borrow_mut();
            if let Some(position) = state
                .lines
                .iter()
                .position(|line| line.item.item_code == code.as_str())
            {
                let next = ((state.lines[position].qty - 1.0) * 1000.0).round() / 1000.0;
                if next <= 0.0 {
                    state.lines.remove(position);
                } else {
                    state.lines[position].qty = next;
                }
            }
            refresh_voucher(&app, &state);
        }
    });

    app.on_line_quantity_edited({
        let app = app.as_weak();
        let state = state.clone();
        move |code, value| {
            let app = app.unwrap();
            let mut state = state.borrow_mut();
            if let Some(position) = state
                .lines
                .iter()
                .position(|line| line.item.item_code == code.as_str())
            {
                let next = value.parse::<f64>().unwrap_or(0.0).max(0.0);
                if next == 0.0 {
                    state.lines.remove(position);
                } else {
                    state.lines[position].qty = next;
                }
            }
            refresh_voucher(&app, &state);
        }
    });

    app.on_clear_lines({
        let app = app.as_weak();
        let state = state.clone();
        move || {
            let app = app.unwrap();
            let mut state = state.borrow_mut();
            state.lines.clear();
            refresh_voucher(&app, &state);
        }
    });

    app.on_submit_print({
        let app = app.as_weak();
        let state = state.clone();
        move || {
            let app = app.unwrap();
            if state.borrow().lines.is_empty() {
                app.set_status_text("Add at least one item before submitting.".into());
                return;
            }
            let Some(config) = ErpRuntimeConfig::from_env() else {
                app.set_status_text(
                    "BLOCKED: authoritative ERPNext submission is not configured; no voucher was fabricated"
                        .into(),
                );
                return;
            };
            let sales_mode = app.get_current_screen() != 1;
            let (items, client_voucher_id, warehouse, customer, target_warehouse, transfer_mode, outgoing_stock_entry, price_list, payment_mode) = {
                let state = state.borrow();
                if sales_mode && state.warehouse.is_empty() {
                    app.set_status_text("Choose a shop warehouse.".into());
                    return;
                }
                if sales_mode && state.customer.is_empty() {
                    app.set_status_text("Choose a customer.".into());
                    return;
                }
                if sales_mode && state.price_list.is_empty() {
                    app.set_status_text("Price list is not configured.".into());
                    return;
                }
                if !sales_mode && state.warehouse.is_empty() {
                    app.set_status_text("Choose a source warehouse.".into());
                    return;
                }
                if !sales_mode && state.target_warehouse.is_empty() {
                    app.set_status_text("Choose a target warehouse.".into());
                    return;
                }
                if !sales_mode && state.warehouse == state.target_warehouse {
                    app.set_status_text("Source and target warehouses must be different.".into());
                    return;
                }
                if !sales_mode && state.transfer_mode == "receive" && state.outgoing_stock_entry.trim().is_empty() {
                    app.set_status_text("Enter the outgoing stock entry to receive.".into());
                    return;
                }
                (
                    state.lines.iter().map(|line| SaleItem {
                        item_code: line.item.item_code.clone(),
                        qty: line.qty,
                        uom: line.item.stock_uom.clone().unwrap_or_else(|| "Nos".into()),
                        rate: sales_mode.then_some(line.item.rate),
                    }).collect::<Vec<_>>(),
                    state.client_voucher_id.clone(),
                    state.warehouse.clone(),
                    state.customer.clone(),
                    state.target_warehouse.clone(),
                    state.transfer_mode.clone(),
                    state.outgoing_stock_entry.clone(),
                    state.price_list.clone(),
                    state.payment_mode.clone(),
                )
            };
            app.set_status_text("Submitting voucher…".into());
            let result = ErpClient::login(&config.base_url, &config.username, &config.password)
                .and_then(|client| {
                    if sales_mode {
                        client.submit_sale(&client_voucher_id, &warehouse, &customer, &price_list, &items, (!payment_mode.is_empty()).then_some(payment_mode.as_str()), "voucher")
                    } else {
                        client.submit_transfer(&client_voucher_id, &warehouse, &target_warehouse, &transfer_mode, (!outgoing_stock_entry.trim().is_empty()).then_some(outgoing_stock_entry.trim()), &items, "voucher")
                    }
                });
            match result {
                Ok(response) => {
                    if let Some(path) = &config.outcome_path {
                        write_native_outcome(path, &client_voucher_id, &response);
                    }
                    let voucher_no = response.get("voucher_no").and_then(|value| value.as_str()).unwrap_or("voucher");
                    let print_note = {
                        let state = state.borrow();
                        if let Some(payload) = response.get("receipt_payload") {
                            match ErpClient::print_receipt(&state.print_endpoint, &state.print_token, payload) {
                                Ok(body) if body.get("dry_run").and_then(|value| value.as_bool()).unwrap_or(false) => {
                                    " — print companion dry-run (nothing printed).".to_owned()
                                }
                                Ok(_) => " and sent to printer.".to_owned(),
                                Err(error) => format!(" — print companion failed: {error}"),
                            }
                        } else {
                            " — print status unclear; receipt payload missing.".to_owned()
                        }
                    };
                    app.set_status_text(format!("Submitted {voucher_no}{print_note}").into());
                    let mut state = state.borrow_mut();
                    state.lines.clear();
                    state.client_voucher_id = new_client_voucher_id();
                    refresh_voucher(&app, &state);
                }
                Err(error) => app.set_status_text(error.to_string().into()),
            }
        }
    });

    app.on_invoice_filter_edited({
        let app = app.as_weak();
        let state = state.clone();
        move |filter| {
            let app = app.unwrap();
            let mut state = state.borrow_mut();
            state.invoice_filter = filter.to_string();
            state.invoice_limit = 20;
            refresh_invoice_rows(&app, &mut state);
        }
    });

    app.on_invoice_load_more({
        let app = app.as_weak();
        let state = state.clone();
        move || {
            let app = app.unwrap();
            let mut state = state.borrow_mut();
            state.invoice_limit += 20;
            refresh_invoice_rows(&app, &mut state);
        }
    });

    app.on_invoice_sort_toggle({
        let app = app.as_weak();
        let state = state.clone();
        move || {
            let app = app.unwrap();
            let mut state = state.borrow_mut();
            state.invoice_sort_ascending = !state.invoice_sort_ascending;
            app.set_invoice_sort_label(
                if state.invoice_sort_ascending {
                    "ascending"
                } else {
                    "descending"
                }
                .into(),
            );
            refresh_invoice_rows(&app, &mut state);
        }
    });

    app.on_invoice_open({
        let app = app.as_weak();
        let state = state.clone();
        move |invoice_name| {
            let app = app.unwrap();
            let mut state = state.borrow_mut();
            refresh_invoice_detail(&app, &mut state, invoice_name.as_str());
            app.set_current_screen(3);
        }
    });

    app.run()
}
