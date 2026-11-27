use khitmee_print_companion::config::Config;
use khitmee_print_companion::server::serve;
use khitmee_print_companion::win_printer::DryRunBackend;
use serde_json::Value;
use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::{Shutdown, TcpStream};
use std::path::PathBuf;
use std::sync::Arc;
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tiny_http::Server;

struct HttpResponse {
    status: u16,
    headers: HashMap<String, String>,
    body: String,
}

fn sample_payload(printer_type: &str) -> String {
    format!(
        r##"{{
            "printer_type": "{printer_type}",
            "voucher_no": "SINV-0001",
            "title": "Khit Me Sales Voucher",
            "shop_name": "Dist Hub 1",
            "date": "2026-11-26 16:00:00",
            "party": "Walk-in Customer",
            "lines": [
                {{"name": "Product Sample A", "qty": 5, "rate": 11760, "amount": 58800}}
            ],
            "total": 58800,
            "footer": "Thank you!"
        }}"##
    )
}

fn start_test_server() -> (u16, PathBuf) {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let dump_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("target")
        .join("test-tmp")
        .join(format!("http-{}-{nanos}", std::process::id()));

    let mut config = Config::default();
    config.port = 0;
    config.token = "test-token".to_string();
    config.dry_run = true;
    config.dump_dir = dump_dir.clone();
    config.voucher_printer_name = "Voucher Test Printer".to_string();
    config.a4_printer_name = "A4 Test Printer".to_string();

    let backend = Arc::new(DryRunBackend::new(&dump_dir));
    let server = Server::http("127.0.0.1:0").unwrap();
    let port = server.server_addr().to_ip().unwrap().port();
    thread::spawn(move || serve(server, config, backend));
    (port, dump_dir)
}

fn http_request(
    port: u16,
    method: &str,
    path: &str,
    headers: &[(&str, &str)],
    body: &str,
) -> HttpResponse {
    let mut stream = TcpStream::connect(("127.0.0.1", port)).unwrap();
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    stream
        .set_write_timeout(Some(Duration::from_secs(5)))
        .unwrap();

    let mut request = format!(
        "{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n"
    );
    if method == "POST" && !headers.iter().any(|(name, _)| name.eq_ignore_ascii_case("content-type")) {
        request.push_str("Content-Type: application/json\r\n");
    }
    for (name, value) in headers {
        request.push_str(&format!("{name}: {value}\r\n"));
    }
    if method == "POST" {
        request.push_str(&format!("Content-Length: {}\r\n", body.len()));
    }
    request.push_str("\r\n");
    request.push_str(body);

    stream.write_all(request.as_bytes()).unwrap();
    let _ = stream.shutdown(Shutdown::Write);
    let mut response = Vec::new();
    stream.read_to_end(&mut response).unwrap();
    let response = String::from_utf8_lossy(&response).into_owned();
    let (head, body) = response.split_once("\r\n\r\n").unwrap_or((&response, ""));
    let mut lines = head.lines();
    let status = lines
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .and_then(|status| status.parse::<u16>().ok())
        .unwrap();
    let headers = lines
        .filter_map(|line| line.split_once(':'))
        .map(|(name, value)| (name.to_ascii_lowercase(), value.trim().to_string()))
        .collect();

    HttpResponse {
        status,
        headers,
        body: body.to_string(),
    }
}

fn artifact_extensions(dir: &std::path::Path) -> Vec<String> {
    let mut extensions = std::fs::read_dir(dir)
        .unwrap()
        .map(|entry| {
            entry
                .unwrap()
                .file_name()
                .to_string_lossy()
                .into_owned()
        })
        .filter(|name| {
            name.ends_with(".escpos.bin") || name.ends_with(".txt") || name.ends_with(".a4.html")
        })
        .collect::<Vec<_>>();
    extensions.sort();
    extensions
}

#[test]
fn health_reports_contract_fields() {
    let (port, _) = start_test_server();
    let response = http_request(port, "GET", "/health", &[], "");
    assert_eq!(response.status, 200);
    let json: Value = serde_json::from_str(&response.body).unwrap();
    assert_eq!(json["status"], "ok");
    assert_eq!(json["version"], "0.1.0");
    assert_eq!(json["dry_run"], true);
    assert_eq!(json["voucher_printer"], "Voucher Test Printer");
    assert_eq!(json["a4_printer"], "A4 Test Printer");
}

#[test]
fn printers_returns_dry_run_printer_list() {
    let (port, _) = start_test_server();
    let response = http_request(port, "GET", "/printers", &[], "");
    assert_eq!(response.status, 200);
    let json: Value = serde_json::from_str(&response.body).unwrap();
    let printers = json["printers"].as_array().unwrap();
    assert!(printers.iter().any(|name| name == "DRY-RUN Voucher Printer"));
    assert!(printers.iter().any(|name| name == "DRY-RUN A4 Printer"));
}

#[test]
fn cors_preflight_allows_configured_origin_and_print_token_header() {
    let (port, _) = start_test_server();
    let response = http_request(
        port,
        "OPTIONS",
        "/print",
        &[
            ("Origin", "http://127.0.0.1:18085"),
            ("Access-Control-Request-Method", "POST"),
            ("Access-Control-Request-Headers", "X-Khitmee-Print-Token"),
        ],
        "",
    );

    assert_eq!(response.status, 204);
    assert_eq!(
        response.headers.get("access-control-allow-origin").unwrap(),
        "http://127.0.0.1:18085"
    );
    assert!(response
        .headers
        .get("access-control-allow-headers")
        .unwrap()
        .contains("X-Khitmee-Print-Token"));
    assert!(response
        .headers
        .get("access-control-allow-methods")
        .unwrap()
        .contains("POST"));
}

#[test]
fn disallowed_origin_is_rejected_even_for_health() {
    let (port, _) = start_test_server();
    let response = http_request(
        port,
        "GET",
        "/health",
        &[("Origin", "http://evil.example")],
        "",
    );

    assert_eq!(response.status, 403);
    assert!(!response.headers.contains_key("access-control-allow-origin"));
}

#[test]
fn print_requires_exact_token() {
    let (port, _) = start_test_server();
    let body = sample_payload("voucher");

    let missing = http_request(port, "POST", "/print", &[], &body);
    assert_eq!(missing.status, 401);

    let wrong = http_request(
        port,
        "POST",
        "/print",
        &[("X-Khitmee-Print-Token", "wrong-token")],
        &body,
    );
    assert_eq!(wrong.status, 401);
}

#[test]
fn valid_voucher_print_writes_escpos_binary_and_text_preview() {
    let (port, dump_dir) = start_test_server();
    let response = http_request(
        port,
        "POST",
        "/print",
        &[
            ("X-Khitmee-Print-Token", "test-token"),
            ("Origin", "http://127.0.0.1:18085"),
        ],
        &sample_payload("voucher"),
    );

    assert_eq!(response.status, 200, "body: {}", response.body);
    let json: Value = serde_json::from_str(&response.body).unwrap();
    assert_eq!(json["status"], "ok");
    assert_eq!(json["dry_run"], true);
    assert_eq!(json["artifacts"].as_array().unwrap().len(), 2);
    let artifacts = artifact_extensions(&dump_dir);
    assert!(artifacts.iter().any(|name| name.ends_with(".escpos.bin")), "{artifacts:?}");
    assert!(artifacts.iter().any(|name| name.ends_with(".txt")), "{artifacts:?}");
}

#[test]
fn valid_a4_print_writes_self_contained_html() {
    let (port, dump_dir) = start_test_server();
    let response = http_request(
        port,
        "POST",
        "/print",
        &[("X-Khitmee-Print-Token", "test-token")],
        &sample_payload("a4"),
    );

    assert_eq!(response.status, 200, "body: {}", response.body);
    let artifacts = artifact_extensions(&dump_dir);
    let html_name = artifacts
        .iter()
        .find(|name| name.ends_with(".a4.html"))
        .expect("expected .a4.html artifact");
    let html = std::fs::read_to_string(dump_dir.join(html_name)).unwrap();
    assert!(html.contains("@page { size: A4;"));
    assert!(html.contains("Product Sample A"));
}

#[test]
fn malformed_receipt_json_is_rejected() {
    let (port, _) = start_test_server();
    let response = http_request(
        port,
        "POST",
        "/print",
        &[("X-Khitmee-Print-Token", "test-token")],
        "{not-json",
    );
    assert_eq!(response.status, 400);
    assert!(response.body.contains("invalid receipt payload"));
}
