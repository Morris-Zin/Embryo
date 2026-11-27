use crate::config::Config;
use crate::format_a4::render_a4_html;
use crate::format_thermal::{render_escpos, render_text_preview};
use crate::models::{ErrorResponse, HealthResponse, PrinterType, PrintersResponse, PrintResponse, ReceiptPayload};
use crate::win_printer::PrinterBackend;
use serde::Serialize;
use std::io::{Cursor, Read};
use std::net::{Ipv4Addr, SocketAddr};
use std::sync::Arc;
use tiny_http::{Header, Method, Request, Response, Server};

const MAX_BODY_BYTES: usize = 1024 * 1024;

pub fn bind_and_serve(
    config: Config,
    backend: Box<dyn PrinterBackend>,
) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let address = SocketAddr::from((Ipv4Addr::LOCALHOST, config.port));
    let server = Server::http(address)?;
    let backend: Arc<dyn PrinterBackend> = backend.into();
    serve(server, config, backend);
    Ok(())
}

pub fn serve(server: Server, config: Config, backend: Arc<dyn PrinterBackend>) {
    for request in server.incoming_requests() {
        handle_request(request, &config, backend.as_ref());
    }
}

pub fn handle_request(mut request: Request, config: &Config, backend: &dyn PrinterBackend) {
    let method = request.method().clone();
    let path = request
        .url()
        .split('?')
        .next()
        .unwrap_or(request.url())
        .to_string();

    let response = match validated_origin(&request, config) {
        Err(()) => error_response(403, "origin is not allowed", None),
        Ok(origin) if method == Method::Options => empty_response(204, origin.as_deref(), true),
        Ok(origin) => route_request(
            &mut request,
            method,
            &path,
            config,
            backend,
            origin.as_deref(),
        ),
    };

    let _ = request.respond(response);
}

fn route_request(
    request: &mut Request,
    method: Method,
    path: &str,
    config: &Config,
    backend: &dyn PrinterBackend,
    origin: Option<&str>,
) -> Response<Cursor<Vec<u8>>> {
    if method == Method::Get && path == "/health" {
        let body = HealthResponse {
            status: "ok",
            version: env!("CARGO_PKG_VERSION"),
            dry_run: backend.is_dry_run(),
            voucher_printer: config.voucher_printer_name.clone(),
            a4_printer: config.a4_printer_name.clone(),
        };
        return json_response(200, &body, origin);
    }

    if method == Method::Get && path == "/printers" {
        return match backend.list_printers() {
            Ok(printers) => json_response(200, &PrintersResponse { printers }, origin),
            Err(err) => error_response(500, &format!("failed to list printers: {err}"), origin),
        };
    }

    if method == Method::Post && path == "/print" {
        return handle_print(request, config, backend, origin);
    }

    if matches!(path, "/health" | "/printers" | "/print") {
        return error_response(405, "method not allowed", origin);
    }

    error_response(404, "not found", origin)
}

fn handle_print(
    request: &mut Request,
    config: &Config,
    backend: &dyn PrinterBackend,
    origin: Option<&str>,
) -> Response<Cursor<Vec<u8>>> {
    let supplied_token = header_value(request.headers(), "X-Khitmee-Print-Token").unwrap_or("");
    if config.token.is_empty() || !constant_time_eq(supplied_token.as_bytes(), config.token.as_bytes()) {
        return error_response(401, "missing or invalid print token", origin);
    }

    let mut body = String::new();
    let mut limited = request.as_reader().take((MAX_BODY_BYTES + 1) as u64);
    if limited.read_to_string(&mut body).is_err() {
        return error_response(400, "request body must be valid UTF-8", origin);
    }
    if body.len() > MAX_BODY_BYTES {
        return error_response(413, "request body is too large", origin);
    }

    let payload = match serde_json::from_str::<ReceiptPayload>(&body) {
        Ok(payload) => payload,
        Err(err) => return error_response(400, &format!("invalid receipt payload: {err}"), origin),
    };
    if let Err(err) = payload.validate() {
        return error_response(400, &err, origin);
    }

    let result = match payload.printer_type {
        PrinterType::Voucher => {
            let escpos = render_escpos(&payload, config.paper_width_chars);
            let preview = render_text_preview(&payload, config.paper_width_chars);
            backend.print_voucher(
                &config.voucher_printer_name,
                &payload.voucher_no,
                &escpos,
                &preview,
            )
        }
        PrinterType::A4 => {
            let html = render_a4_html(&payload);
            backend.print_a4(&config.a4_printer_name, &payload.voucher_no, &html)
        }
    };

    match result {
        Ok(artifacts) => json_response(
            200,
            &PrintResponse {
                status: "ok",
                voucher_no: payload.voucher_no,
                printer_type: payload.printer_type,
                dry_run: backend.is_dry_run(),
                artifacts: artifacts
                    .iter()
                    .map(|path| path.to_string_lossy().into_owned())
                    .collect(),
            },
            origin,
        ),
        Err(err) => error_response(500, &format!("print failed: {err}"), origin),
    }
}

fn validated_origin(request: &Request, config: &Config) -> Result<Option<String>, ()> {
    match header_value(request.headers(), "Origin") {
        Some(origin) if config.allowed_origins.iter().any(|allowed| allowed == origin) => {
            Ok(Some(origin.to_string()))
        }
        Some(_) => Err(()),
        None => Ok(None),
    }
}

fn header_value<'a>(headers: &'a [Header], name: &str) -> Option<&'a str> {
    headers
        .iter()
        .find(|header| header.field.as_str().as_str().eq_ignore_ascii_case(name))
        .map(|header| header.value.as_str())
}

fn json_response<T: Serialize>(
    status: u16,
    body: &T,
    origin: Option<&str>,
) -> Response<Cursor<Vec<u8>>> {
    let bytes = serde_json::to_vec(body).unwrap_or_else(|_| b"{\"error\":\"serialization failed\"}".to_vec());
    response(status, bytes, origin, false, "application/json")
}

fn error_response(status: u16, message: &str, origin: Option<&str>) -> Response<Cursor<Vec<u8>>> {
    json_response(
        status,
        &ErrorResponse {
            error: message.to_string(),
        },
        origin,
    )
}

fn empty_response(status: u16, origin: Option<&str>, preflight: bool) -> Response<Cursor<Vec<u8>>> {
    response(status, Vec::new(), origin, preflight, "text/plain")
}

fn response(
    status: u16,
    body: Vec<u8>,
    origin: Option<&str>,
    preflight: bool,
    content_type: &str,
) -> Response<Cursor<Vec<u8>>> {
    let mut response = Response::from_data(body)
        .with_status_code(status)
        .with_header(make_header("Content-Type", content_type))
        .with_header(make_header("Vary", "Origin"))
        .with_header(make_header("X-Content-Type-Options", "nosniff"));

    if let Some(origin) = origin {
        response = response.with_header(make_header("Access-Control-Allow-Origin", origin));
    }
    if preflight {
        response = response
            .with_header(make_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS"))
            .with_header(make_header(
                "Access-Control-Allow-Headers",
                "Content-Type, X-Khitmee-Print-Token",
            ))
            .with_header(make_header("Access-Control-Max-Age", "600"));
    }
    response
}

fn make_header(name: &str, value: &str) -> Header {
    Header::from_bytes(name.as_bytes(), value.as_bytes()).expect("static ASCII response header")
}

fn constant_time_eq(left: &[u8], right: &[u8]) -> bool {
    if left.len() != right.len() {
        return false;
    }
    left.iter()
        .zip(right.iter())
        .fold(0u8, |diff, (a, b)| diff | (a ^ b))
        == 0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn constant_time_comparison_matches_only_equal_bytes() {
        assert!(constant_time_eq(b"secret", b"secret"));
        assert!(!constant_time_eq(b"secret", b"secreu"));
        assert!(!constant_time_eq(b"secret", b"secret-longer"));
    }

    #[test]
    fn parses_allowed_origin_exactly() {
        let config = Config::default();
        assert!(config.allowed_origins.contains(&"http://127.0.0.1:18085".to_string()));
        assert!(!config.allowed_origins.contains(&"http://127.0.0.1:18085/".to_string()));
    }
}
