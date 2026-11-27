#[cfg(feature = "encrypted-catalog")]
use khitmee_catalog::{hex_key, load_or_create_key, EncryptedStore};
use khitmee_catalog::{CatalogIndex, CatalogItem};
use slint::{Model, ModelRc, SharedString, VecModel};
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
}

#[derive(Clone)]
struct InvoiceSeed {
    name: String,
    customer: String,
    posting_date: String,
    total: f64,
    status: String,
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

fn fixture_catalog(rows: usize) -> Vec<CatalogItem> {
    let burmese = [
        "ဆန်၀ါး ၅ ကီလို",
        "နံနံပင်ခြောက်",
        "ကြက်ဥ အကြီး ၁၀ လုံး",
        "နို့ဆီ ၁ လိတာ",
        "မန်ကျည်းသီး အေးမြသီး",
    ];
    (0..rows)
        .map(|index| {
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
    (0..rows)
        .map(|index| InvoiceSeed {
            name: format!("SINV-2026-{index:06}"),
            customer: if index % 1000 == 0 {
                format!("မြတ်သူ {:04}", index % 1000)
            } else {
                format!("Customer {:04}", index % 997)
            },
            posting_date: format!("2026-{:02}-{:02}", 1 + index % 11, 1 + index % 28),
            total: 1000.0 + ((index * 173) % 85_000) as f64 / 2.0,
            status: match index % 5 {
                0 => "Paid",
                1 => "Unpaid",
                2 => "Overdue",
                3 => "Draft",
                _ => "Return",
            }
            .into(),
        })
        .collect()
}

fn search_model(index: &CatalogIndex, term: &str) -> ModelRc<CatalogChoice> {
    let rows = index
        .search(term, 25)
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

fn voucher_model(lines: &[LineState]) -> ModelRc<VoucherLine> {
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
            rate_text: money(line.item.rate).into(),
            amount_text: money(line.item.rate * line.qty).into(),
        })
        .collect::<Vec<_>>();
    Rc::new(VecModel::from(rows)).into()
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
            total_text: money(row.total).into(),
            status: row.status.clone().into(),
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
    app.set_voucher_lines(voucher_model(&state.lines));
    let total_qty: f64 = state.lines.iter().map(|line| line.qty).sum();
    let subtotal: f64 = state
        .lines
        .iter()
        .map(|line| line.qty * line.item.rate)
        .sum();
    app.set_total_qty_text(qty(total_qty).into());
    app.set_subtotal_text(money(subtotal).into());
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
    let sync_token = format!("fixture-v1:{rows}");
    if store.sync_token(&scope)? != sync_token || store.item_count(&scope)? != rows {
        store.apply_sync(&scope, true, &sync_token, &fixture_catalog(rows))?;
    }
    store.rows(&scope)
}

#[cfg(not(feature = "encrypted-catalog"))]
fn load_fixture_catalog(
    rows: usize,
) -> Result<Vec<CatalogItem>, Box<dyn std::error::Error + Send + Sync>> {
    Ok(fixture_catalog(rows))
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

    let stored_rows =
        load_fixture_catalog(catalog_rows).expect("open the encrypted fixture catalog store");
    let by_code = stored_rows
        .iter()
        .map(|row| (row.item_code.clone(), row.clone()))
        .collect::<HashMap<_, _>>();
    let catalog = CatalogIndex::rebuild(stored_rows);
    let state = Rc::new(RefCell::new(PilotState {
        catalog_size: catalog.len(),
        catalog,
        by_code,
        lines: Vec::new(),
        invoices: fixture_invoices(invoice_rows),
        invoice_filter: String::new(),
    }));

    let app = AppWindow::new()?;
    app.set_fixture_note(
        SharedString::from(format!(
            "Fixture-only qualification data: {catalog_rows} catalog rows and {invoice_rows} invoice rows. ERPNext parity is pending live reference capture."
        ))
    );
    app.set_invoice_rows(invoice_model(&state.borrow().invoices, ""));
    app.set_search_results(search_model(&state.borrow().catalog, "retail"));
    refresh_voucher(&app, &state.borrow());

    app.on_search_edited({
        let app = app.as_weak();
        let state = state.clone();
        move |term| {
            let app = app.unwrap();
            let state = state.borrow();
            app.set_search_results(search_model(&state.catalog, term.as_str()));
            app.set_status_text(SharedString::from(format!(
                "Local catalog search: {} visible matches from {} rows",
                app.get_search_results().row_count(),
                state.catalog_size
            )));
        }
    });

    app.on_sales_scan_accepted({
        let app = app.as_weak();
        let state = state.clone();
        move |term| {
            let app = app.unwrap();
            let mut state = state.borrow_mut();
            let matches = state.catalog.search(term.as_str(), 2);
            app.set_clear_scan_after_accept(matches.len() == 1);
            if matches.len() == 1 {
                let item = matches[0].clone();
                let item_code = item.item_code.clone();
                add_item_to_draft(&mut state, item);
                refresh_voucher(&app, &state);
                app.set_status_text(SharedString::from(format!(
                    "Scan accepted: {item_code} added to local draft; ERPNext submission still required"
                )));
            } else if matches.is_empty() {
                app.set_status_text("Scan/search matched no item in the local catalog snapshot".into());
            } else {
                app.set_status_text("Scan/search is ambiguous; narrow the term or choose a result".into());
            }
        }
    });

    app.on_choose_item({
        let app = app.as_weak();
        let state = state.clone();
        move |code| {
            let app = app.unwrap();
            let mut state = state.borrow_mut();
            if let Some(item) = state.by_code.get(code.as_str()).cloned() {
                add_item_to_draft(&mut state, item);
                app.set_status_text(
                    "Item added to local draft; ERPNext submission still required".into(),
                );
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
            app.set_status_text("Line removed from local draft".into());
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
            app.set_status_text("Local draft cleared".into());
        }
    });

    app.on_submit_print({
        let app = app.as_weak();
        move || {
            let app = app.unwrap();
            app.set_status_text(
                "BLOCKED: authoritative ERPNext submission and printer adapter are not configured; no sale was fabricated"
                    .into(),
            );
        }
    });

    app.on_invoice_filter_edited({
        let app = app.as_weak();
        let state = state.clone();
        move |filter| {
            let app = app.unwrap();
            let mut state = state.borrow_mut();
            state.invoice_filter = filter.to_string();
            app.set_invoice_rows(invoice_model(&state.invoices, &state.invoice_filter));
            app.set_status_text(SharedString::from(format!(
                "Invoice fixture filter: {} visible rows",
                app.get_invoice_rows().row_count()
            )));
        }
    });

    app.run()
}
