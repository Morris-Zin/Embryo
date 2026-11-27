#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use khitmee_catalog_service::keyring::{hex_key, load_or_create_key};
use khitmee_catalog_service::{bind_and_serve, EncryptedStore, ServiceConfig, ServiceState};
use std::net::{Ipv4Addr, SocketAddr, TcpStream};
use std::path::PathBuf;
use std::time::Duration;
use tauri::{WebviewUrl, WebviewWindowBuilder};

fn data_dir() -> PathBuf {
    if cfg!(windows) {
        if let Ok(appdata) = std::env::var("APPDATA") {
            return PathBuf::from(appdata).join("Khitmee");
        }
    }
    if let Ok(home) = std::env::var("HOME") {
        return PathBuf::from(home).join(".khitmee");
    }
    std::env::temp_dir().join("khitmee")
}

fn loopback_service_running(port: u16) -> bool {
    let address = SocketAddr::from((Ipv4Addr::LOCALHOST, port));
    TcpStream::connect_timeout(&address, Duration::from_millis(150)).is_ok()
}

fn catalog_port() -> u16 {
    std::env::var("KHITMEE_CATALOG_PORT")
        .ok()
        .and_then(|value| value.parse::<u16>().ok())
        .unwrap_or(18422)
}

fn start_catalog_service() {
    let port = catalog_port();
    if loopback_service_running(port) {
        return;
    }
    std::thread::spawn(move || {
        let dir = data_dir();
        let key_path = dir.join("catalog.key");
        let db_path = dir.join("catalog.sqlcipher.db");
        let result = load_or_create_key(&key_path).and_then(|key| {
            let store = EncryptedStore::open(&db_path, &hex_key(&key))?;
            let config = ServiceConfig {
                port,
                allowed_origins: std::env::var("KHITMEE_ALLOWED_ORIGINS")
                    .unwrap_or_else(|_| "http://127.0.0.1:18086".to_string())
                    .split(',')
                    .map(|origin| origin.trim().to_string())
                    .filter(|origin| !origin.is_empty())
                    .collect(),
                token: std::env::var("KHITMEE_DESKTOP_TOKEN").ok(),
            };
            bind_and_serve(config, ServiceState::new(store))
        });
        if let Err(error) = result {
            eprintln!("catalog service failed: {error}");
        }
    });
}

fn target_url() -> Result<url::Url, url::ParseError> {
    let raw = std::env::var("KHITMEE_SERVER_URL")
        .unwrap_or_else(|_| "http://127.0.0.1:18086/khitmee_sales".to_string());
    let mut url = url::Url::parse(&raw)?;
    if std::env::var("KHITMEE_DESKTOP_MODE").unwrap_or_else(|_| "rust".to_string()) == "rust" {
        let mut pairs = url.query_pairs_mut();
        pairs.append_pair("khitmee_catalog", "rust");
        let port = catalog_port();
        if port != 18422 {
            pairs.append_pair("khitmee_catalog_port", &port.to_string());
        }
    }
    Ok(url)
}

fn main() {
    let mode = std::env::var("KHITMEE_DESKTOP_MODE").unwrap_or_else(|_| "rust".to_string());
    if mode == "rust" {
        start_catalog_service();
    }

    tauri::Builder::default()
        .setup(|app| {
            let url = target_url()?;
            WebviewWindowBuilder::new(app, "main", WebviewUrl::External(url))
                .title("Khitmee Counter")
                .inner_size(1360.0, 850.0)
                .build()?;
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("failed to run Khitmee Tauri shell");
}
