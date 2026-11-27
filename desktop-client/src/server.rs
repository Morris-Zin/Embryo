use crate::index::CatalogIndex;
use crate::models::{
    ErrorResponse, HealthResponse, SearchResponse, StateResponse, SyncPayload, SyncResponse,
};
use crate::storage::EncryptedStore;
use serde::Serialize;
use std::collections::HashMap;
use std::error::Error;
use std::io::{Cursor, Read};
use std::net::{Ipv4Addr, SocketAddr};
use std::sync::{Arc, RwLock};
use tiny_http::{Header, Method, Request, Response, Server};
use url::Url;

const MAX_BODY_BYTES: usize = 24 * 1024 * 1024;
const MAX_SCOPE_BYTES: usize = 512;
const MAX_QUERY_BYTES: usize = 256;

pub struct ServiceConfig {
    pub port: u16,
    pub allowed_origins: Vec<String>,
    pub token: Option<String>,
}

pub struct ServiceState {
    pub store: EncryptedStore,
    pub catalogs: RwLock<HashMap<String, CatalogIndex>>,
}

impl ServiceState {
    pub fn new(store: EncryptedStore) -> Self {
        Self {
            store,
            catalogs: RwLock::new(HashMap::new()),
        }
    }

    fn ensure_index(&self, scope: &str) -> Result<(), Box<dyn Error + Send + Sync>> {
        if self
            .catalogs
            .read()
            .map_err(|_| "catalog lock poisoned")?
            .contains_key(scope)
        {
            return Ok(());
        }
        let rows = self.store.rows(scope)?;
        let index = CatalogIndex::rebuild(rows);
        self.catalogs
            .write()
            .map_err(|_| "catalog lock poisoned")?
            .insert(scope.to_string(), index);
        Ok(())
    }

    fn search(
        &self,
        scope: &str,
        term: &str,
        limit: usize,
    ) -> Result<Vec<crate::models::CatalogItem>, Box<dyn Error + Send + Sync>> {
        self.ensure_index(scope)?;
        let catalogs = self.catalogs.read().map_err(|_| "catalog lock poisoned")?;
        Ok(catalogs
            .get(scope)
            .map(|index| index.search(term, limit))
            .unwrap_or_default())
    }

    fn apply_sync(&self, payload: SyncPayload) -> Result<usize, Box<dyn Error + Send + Sync>> {
        let count = self.store.apply_sync(
            &payload.scope,
            payload.reset,
            &payload.sync_token,
            &payload.items,
        )?;
        let rows = self.store.rows(&payload.scope)?;
        self.catalogs
            .write()
            .map_err(|_| "catalog lock poisoned")?
            .insert(payload.scope, CatalogIndex::rebuild(rows));
        Ok(count)
    }

    fn totals(&self) -> (usize, usize) {
        let catalogs = self.catalogs.read().map(|values| {
            (
                values.len(),
                values.values().map(|index| index.len()).sum::<usize>(),
            )
        });
        catalogs.unwrap_or((0, 0))
    }
}

pub fn bind_and_serve(
    config: ServiceConfig,
    state: ServiceState,
) -> Result<(), Box<dyn Error + Send + Sync>> {
    let address = SocketAddr::from((Ipv4Addr::LOCALHOST, config.port));
    let server = Server::http(address)?;
    serve(server, config, Arc::new(state));
    Ok(())
}

pub fn serve(server: Server, config: ServiceConfig, state: Arc<ServiceState>) {
    for request in server.incoming_requests() {
        handle_request(request, &config, state.clone());
    }
}

fn handle_request(mut request: Request, config: &ServiceConfig, state: Arc<ServiceState>) {
    let method = request.method().clone();
    let url_text = request.url().to_string();
    let parsed = Url::parse(&format!("http://127.0.0.1{url_text}"));
    let response = match (parsed, validated_origin(&request, config)) {
        (Err(_), _) => error_response(400, "invalid request URL", None),
        (_, Err(())) => error_response(403, "origin is not allowed", None),
        (Ok(_url), Ok(origin)) if method == Method::Options => {
            empty_response(204, origin.as_deref(), true)
        }
        (Ok(url), Ok(origin)) => {
            route_request(&mut request, method, url, config, state, origin.as_deref())
        }
    };
    let _ = request.respond(response);
}

fn route_request(
    request: &mut Request,
    method: Method,
    url: Url,
    config: &ServiceConfig,
    state: Arc<ServiceState>,
    origin: Option<&str>,
) -> Response<Cursor<Vec<u8>>> {
    let path = url.path();
    if method == Method::Get && path == "/health" {
        let (catalogs, items) = state.totals();
        return json_response(
            200,
            &HealthResponse {
                status: "ok",
                version: env!("CARGO_PKG_VERSION"),
                encrypted: true,
                catalogs,
                items,
            },
            origin,
        );
    }

    if !authorized(request, config) {
        return error_response(401, "missing or invalid desktop catalog token", origin);
    }

    if method == Method::Get && path == "/state" {
        let scope = match query_value(&url, "scope") {
            Some(scope) if valid_scoped_request(&scope, origin) => scope,
            _ => return error_response(400, "valid scope is required", origin),
        };
        return match (
            state.store.sync_token(&scope),
            state.store.item_count(&scope),
        ) {
            (Ok(sync_token), Ok(item_count)) => json_response(
                200,
                &StateResponse {
                    sync_token,
                    item_count,
                },
                origin,
            ),
            (Err(err), _) | (_, Err(err)) => {
                error_response(500, &format!("state lookup failed: {err}"), origin)
            }
        };
    }

    if method == Method::Post && path == "/sync" {
        return handle_sync(request, state, origin);
    }

    if method == Method::Get && path == "/search" {
        let scope = match query_value(&url, "scope") {
            Some(scope) if valid_scoped_request(&scope, origin) => scope,
            _ => return error_response(400, "valid scope is required", origin),
        };
        let term = query_value(&url, "q").unwrap_or_default();
        if term.len() > MAX_QUERY_BYTES {
            return error_response(400, "query is too long", origin);
        }
        let limit = query_value(&url, "limit")
            .and_then(|value| value.parse::<usize>().ok())
            .unwrap_or(10)
            .clamp(1, 25);
        return match state.search(&scope, &term, limit) {
            Ok(items) => json_response(200, &SearchResponse { items }, origin),
            Err(err) => error_response(500, &format!("search failed: {err}"), origin),
        };
    }

    if matches!(path, "/state" | "/sync" | "/search") {
        return error_response(405, "method not allowed", origin);
    }
    error_response(404, "not found", origin)
}

fn handle_sync(
    request: &mut Request,
    state: Arc<ServiceState>,
    origin: Option<&str>,
) -> Response<Cursor<Vec<u8>>> {
    let content_type = header_value(request.headers(), "Content-Type").unwrap_or("");
    if !content_type
        .to_ascii_lowercase()
        .starts_with("application/json")
    {
        return error_response(415, "sync requires application/json", origin);
    }
    let mut body = String::new();
    let mut limited = request.as_reader().take((MAX_BODY_BYTES + 1) as u64);
    if limited.read_to_string(&mut body).is_err() {
        return error_response(400, "request body must be valid UTF-8", origin);
    }
    if body.len() > MAX_BODY_BYTES {
        return error_response(413, "request body is too large", origin);
    }
    let payload = match serde_json::from_str::<SyncPayload>(&body) {
        Ok(payload) => payload,
        Err(err) => return error_response(400, &format!("invalid sync payload: {err}"), origin),
    };
    if !valid_scoped_request(&payload.scope, origin) || payload.items.len() > 25_000 {
        return error_response(400, "invalid sync scope or item count", origin);
    }
    match state.apply_sync(payload) {
        Ok(item_count) => json_response(
            200,
            &SyncResponse {
                status: "ok",
                item_count,
            },
            origin,
        ),
        Err(err) => error_response(500, &format!("sync failed: {err}"), origin),
    }
}

fn query_value(url: &Url, key: &str) -> Option<String> {
    url.query_pairs()
        .find(|(name, _)| name == key)
        .map(|(_, value)| value.into_owned())
}

fn valid_scope(scope: &str) -> bool {
    !scope.is_empty()
        && scope.len() <= MAX_SCOPE_BYTES
        && scope
            .chars()
            .all(|char| !char.is_control() || char == '\u{1f}')
}

fn scope_matches_origin(scope: &str, origin: Option<&str>) -> bool {
    match origin {
        Some(origin) => scope.split('\u{1f}').next() == Some(origin),
        None => true,
    }
}

fn valid_scoped_request(scope: &str, origin: Option<&str>) -> bool {
    valid_scope(scope) && scope_matches_origin(scope, origin)
}

fn authorized(request: &Request, config: &ServiceConfig) -> bool {
    match config.token.as_deref() {
        None | Some("") => true,
        Some(expected) => {
            let supplied = header_value(request.headers(), "X-Khitmee-Desktop-Token").unwrap_or("");
            constant_time_eq(supplied.as_bytes(), expected.as_bytes())
        }
    }
}

fn validated_origin(request: &Request, config: &ServiceConfig) -> Result<Option<String>, ()> {
    match header_value(request.headers(), "Origin") {
        Some(origin)
            if config
                .allowed_origins
                .iter()
                .any(|allowed| allowed == "*" || allowed == origin) =>
        {
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
    let bytes = serde_json::to_vec(body)
        .unwrap_or_else(|_| b"{\"error\":\"serialization failed\"}".to_vec());
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
            .with_header(make_header(
                "Access-Control-Allow-Methods",
                "GET, POST, OPTIONS",
            ))
            .with_header(make_header(
                "Access-Control-Allow-Headers",
                "Content-Type, Accept, X-Khitmee-Desktop-Token",
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
    fn scope_must_belong_to_browser_origin() {
        assert!(scope_matches_origin(
            "http://127.0.0.1:18086\u{1f}Shop 1\u{1f}Retail",
            Some("http://127.0.0.1:18086")
        ));
        assert!(!scope_matches_origin(
            "http://127.0.0.1:18085\u{1f}Shop 1\u{1f}Retail",
            Some("http://127.0.0.1:18086")
        ));
        assert!(scope_matches_origin("legacy\u{1f}Shop 1", None));
    }

    #[test]
    fn scope_validation_accepts_unit_separator_but_not_controls() {
        assert!(valid_scope("warehouse\u{1f}price"));
        assert!(!valid_scope("bad\nscope"));
        assert!(!valid_scope(""));
    }
}
