use khitmee_erp::{ErpClient, SaleItem};
use serde::Deserialize;
use serde_json::json;
use std::env;
use std::error::Error;
use std::fs;
use std::path::PathBuf;

#[derive(Deserialize)]
struct SaleFixture {
    warehouse: String,
    customer: String,
    price_list: String,
    items: Vec<SaleItem>,
    payment_mode: Option<String>,
    printer_type: Option<String>,
    target_warehouse: Option<String>,
    transfer_mode: Option<String>,
    outgoing_stock_entry: Option<String>,
}

fn arg_value(args: &[String], name: &str) -> Option<String> {
    args.windows(2).find(|pair| pair[0] == name).map(|pair| pair[1].clone())
}

fn main() -> Result<(), Box<dyn Error + Send + Sync>> {
    let args = env::args().collect::<Vec<_>>();
    let fixture_path = arg_value(&args, "--fixture").ok_or("--fixture is required")?;
    let client_voucher_id = arg_value(&args, "--client-voucher-id").ok_or("--client-voucher-id is required")?;
    let outcome = PathBuf::from(arg_value(&args, "--outcome").ok_or("--outcome is required")?);
    let base_url = arg_value(&args, "--base-url").unwrap_or_else(|| "http://127.0.0.1:18086".into());
    let username = arg_value(&args, "--username").unwrap_or_else(|| "Administrator".into());
    let password = arg_value(&args, "--password").unwrap_or_default();

    let fixture: SaleFixture = serde_json::from_str(&fs::read_to_string(fixture_path)?)?;
    let client = ErpClient::login(&base_url, &username, &password)?;
    let operation = arg_value(&args, "--operation").unwrap_or_else(|| "sale".into());
    let message = if operation == "transfer" {
        client.submit_transfer(
            &client_voucher_id,
            &fixture.warehouse,
            fixture.target_warehouse.as_deref().ok_or("transfer fixture requires target_warehouse")?,
            fixture.transfer_mode.as_deref().unwrap_or("direct"),
            fixture.outgoing_stock_entry.as_deref(),
            &fixture.items,
            fixture.printer_type.as_deref().unwrap_or("voucher"),
        )?
    } else {
        client.submit_sale(
            &client_voucher_id,
            &fixture.warehouse,
            &fixture.customer,
            &fixture.price_list,
            &fixture.items,
            fixture.payment_mode.as_deref(),
            fixture.printer_type.as_deref().unwrap_or("voucher"),
        )?
    };
    if let Some(parent) = outcome.parent() {
        fs::create_dir_all(parent)?;
    }
    fs::write(
        outcome,
        serde_json::to_string_pretty(&json!({
            "schemaVersion": 1,
            "adapter": "khitmee-erp",
            "authoritative": true,
            "used_fixture_substitution": false,
            "client_voucher_id": client_voucher_id,
            "response": message,
            "voucher_no": message.get("voucher_no").cloned().unwrap_or(serde_json::Value::Null),
        }))? + "\n",
    )?;
    Ok(())
}
