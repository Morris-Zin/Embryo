use khitmee_catalog_service::index::CatalogIndex;
use khitmee_catalog_service::keyring::{hex_key, load_or_create_key};
use khitmee_catalog_service::models::CatalogItem;
use khitmee_catalog_service::{bind_and_serve, EncryptedStore, ServiceConfig, ServiceState};
use std::error::Error;
use std::path::PathBuf;
use std::time::Instant;

fn usage() {
    println!(
        "khitmee-catalog-service\n\n\
         Usage:\n\
           khitmee-catalog-service [serve] [--db PATH] [--key PATH] [--port PORT] [--allowed-origin ORIGIN]...\n\
           khitmee-catalog-service benchmark [--rows N] [--iterations N]\n\n\
         Defaults: port 18422, origins http://127.0.0.1:18085 and http://127.0.0.1:18086.\n\
         The SQLCipher key is generated locally and is never embedded in the binary."
    );
}

fn default_data_dir() -> PathBuf {
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

fn option_value(args: &[String], name: &str) -> Option<String> {
    args.windows(2)
        .find(|pair| pair[0] == name)
        .map(|pair| pair[1].clone())
}

fn option_values(args: &[String], name: &str) -> Vec<String> {
    let mut values = Vec::new();
    for (index, arg) in args.iter().enumerate() {
        if arg == name && index + 1 < args.len() {
            values.push(args[index + 1].clone());
        }
    }
    values
}

fn parse_args(args: &[String]) -> (String, ServiceConfig, PathBuf, PathBuf, usize, usize) {
    let command = args.first().cloned().unwrap_or_else(|| "serve".to_string());
    let data_dir = default_data_dir();
    let db = option_value(args, "--db")
        .map(PathBuf::from)
        .unwrap_or_else(|| data_dir.join("catalog.sqlcipher.db"));
    let key = option_value(args, "--key")
        .map(PathBuf::from)
        .unwrap_or_else(|| data_dir.join("catalog.key"));
    let port = option_value(args, "--port")
        .and_then(|value| value.parse::<u16>().ok())
        .unwrap_or(18422);
    let mut allowed_origins = option_values(args, "--allowed-origin");
    if allowed_origins.is_empty() {
        allowed_origins = vec![
            "http://127.0.0.1:18085".to_string(),
            "http://127.0.0.1:18086".to_string(),
        ];
    }
    let rows = option_value(args, "--rows")
        .and_then(|value| value.parse::<usize>().ok())
        .unwrap_or(5000);
    let iterations = option_value(args, "--iterations")
        .and_then(|value| value.parse::<usize>().ok())
        .unwrap_or(500);
    (
        command,
        ServiceConfig {
            port,
            allowed_origins,
            token: option_value(args, "--token")
                .or_else(|| std::env::var("KHITMEE_DESKTOP_TOKEN").ok()),
        },
        db,
        key,
        rows,
        iterations,
    )
}

fn synthetic_items(rows: usize) -> Vec<CatalogItem> {
    (0..rows)
        .map(|index| CatalogItem {
            item_code: format!("KM-{index:05}"),
            item_name: Some(format!("Benchmark Beverage Snack Product {index}")),
            stock_uom: Some("Nos".to_string()),
            item_group: Some(if index % 2 == 0 { "Beverage" } else { "Snack" }.to_string()),
            brand: Some("Benchmark".to_string()),
            barcode: Some(format!("885{index:010}")),
            barcode_text: Some(format!("885{index:010} 990{index:010}")),
            rate: 1000.0 + index as f64,
            actual_qty: (index % 97) as f64,
            modified: Some("benchmark".to_string()),
            deleted: false,
        })
        .collect()
}

fn percentile(values: &mut Vec<f64>, percentile: usize) -> f64 {
    values.sort_by(|left, right| left.partial_cmp(right).unwrap_or(std::cmp::Ordering::Equal));
    if values.is_empty() {
        return 0.0;
    }
    let index = ((percentile as f64 / 100.0) * values.len() as f64).ceil() as usize;
    values[index.saturating_sub(1).min(values.len() - 1)]
}

fn current_rss_mib() -> f64 {
    let status = std::fs::read_to_string("/proc/self/status").unwrap_or_default();
    for line in status.lines() {
        if let Some(value) = line.strip_prefix("VmRSS:") {
            let kb: f64 = value
                .split_whitespace()
                .next()
                .and_then(|entry| entry.parse().ok())
                .unwrap_or(0.0);
            return kb / 1024.0;
        }
    }
    0.0
}

fn benchmark(rows: usize, iterations: usize) -> Result<(), Box<dyn Error + Send + Sync>> {
    let started = Instant::now();
    let items = synthetic_items(rows);
    let build_started = Instant::now();
    let index = CatalogIndex::rebuild(items);
    let build_ms = build_started.elapsed().as_secs_f64() * 1000.0;

    let mut exact = Vec::new();
    let mut search = Vec::new();
    for iteration in 0..iterations {
        let item_index = iteration % rows.max(1);
        let barcode = format!("885{item_index:010}");
        let started_exact = Instant::now();
        let hits = index.search(&barcode, 10);
        exact.push(started_exact.elapsed().as_secs_f64() * 1000.0);
        if hits.is_empty() {
            return Err("benchmark exact barcode miss".into());
        }

        let term = if iteration % 2 == 0 {
            "Beverage"
        } else {
            "KM-00"
        };
        let started_search = Instant::now();
        let _ = index.search(term, 10);
        search.push(started_search.elapsed().as_secs_f64() * 1000.0);
    }
    let binary_bytes = std::env::current_exe()
        .and_then(|path| path.metadata())
        .map(|metadata| metadata.len())
        .unwrap_or(0);
    let result = serde_json::json!({
        "mode": "rust-index-microbenchmark",
        "rows": rows,
        "iterations": iterations,
        "build_ms": (build_ms * 100.0).round() / 100.0,
        "exact_barcode_p50_ms": percentile(&mut exact.clone(), 50),
        "exact_barcode_p95_ms": percentile(&mut exact, 95),
        "search_p50_ms": percentile(&mut search.clone(), 50),
        "search_p95_ms": percentile(&mut search, 95),
        "process_rss_mib": current_rss_mib(),
        "binary_bytes": binary_bytes,
        "total_ms": started.elapsed().as_secs_f64() * 1000.0,
        "simulated": true
    });
    println!("{}", serde_json::to_string_pretty(&result)?);
    Ok(())
}

fn run() -> Result<(), Box<dyn Error + Send + Sync>> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.iter().any(|arg| arg == "--help" || arg == "-h") {
        usage();
        return Ok(());
    }
    let (command, config, db, key_path, rows, iterations) = parse_args(&args);
    if command == "benchmark" {
        return benchmark(rows, iterations);
    }
    if command != "serve" {
        return Err(format!("unknown command {command}").into());
    }

    let key = load_or_create_key(&key_path)?;
    let store = EncryptedStore::open(&db, &hex_key(&key))?;
    let state = ServiceState::new(store);
    eprintln!(
        "khitmee-catalog-service listening on 127.0.0.1:{} (encrypted SQLCipher catalog at {})",
        config.port,
        db.display()
    );
    bind_and_serve(config, state)
}

fn main() {
    if let Err(error) = run() {
        eprintln!("khitmee-catalog-service: {error}");
        std::process::exit(1);
    }
}
