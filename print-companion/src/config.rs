use serde::Deserialize;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, Deserialize)]
#[serde(default)]
pub struct Config {
    pub port: u16,
    pub token: String,
    pub allowed_origins: Vec<String>,
    pub voucher_printer_name: String,
    pub a4_printer_name: String,
    pub paper_width_chars: usize,
    pub dry_run: bool,
    pub dump_dir: PathBuf,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            port: 18421,
            token: String::new(),
            allowed_origins: vec![
                "http://127.0.0.1:18085".to_string(),
                "http://127.0.0.1:18086".to_string(),
            ],
            voucher_printer_name: String::new(),
            a4_printer_name: String::new(),
            paper_width_chars: 48,
            dry_run: true,
            dump_dir: PathBuf::from("print-dump"),
        }
    }
}

impl Config {
    pub fn load(path: &Path) -> io::Result<Self> {
        if !path.exists() {
            return Ok(Self::default());
        }
        let text = fs::read_to_string(path)?;
        toml::from_str(&text).map_err(|err| io::Error::new(io::ErrorKind::InvalidData, err))
    }

    pub fn apply_overrides(
        &mut self,
        port: Option<u16>,
        dry_run: bool,
        dump_dir: Option<PathBuf>,
    ) {
        if let Some(port) = port {
            self.port = port;
        }
        if dry_run {
            self.dry_run = true;
        }
        if let Some(dump_dir) = dump_dir {
            self.dump_dir = dump_dir;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_match_local_companion_contract() {
        let config = Config::default();
        assert_eq!(config.port, 18421);
        assert_eq!(config.paper_width_chars, 48);
        assert!(config.dry_run);
        assert!(config
            .allowed_origins
            .iter()
            .any(|origin| origin == "http://127.0.0.1:18085"));
        assert!(config
            .allowed_origins
            .iter()
            .any(|origin| origin == "http://127.0.0.1:18086"));
    }

    #[test]
    fn parses_complete_toml_config() {
        let text = r##"
port = 18421
token = "local-test-token"
allowed_origins = ["http://127.0.0.1:18085", "https://erp.example.com"]
voucher_printer_name = "POS-80"
a4_printer_name = "Canon A4"
paper_width_chars = 42
dry_run = false
dump_dir = "dump-output"
"##;

        let config: Config = toml::from_str(text).unwrap();

        assert_eq!(config.port, 18421);
        assert_eq!(config.token, "local-test-token");
        assert_eq!(config.allowed_origins.len(), 2);
        assert_eq!(config.voucher_printer_name, "POS-80");
        assert_eq!(config.a4_printer_name, "Canon A4");
        assert_eq!(config.paper_width_chars, 42);
        assert!(!config.dry_run);
        assert_eq!(config.dump_dir, PathBuf::from("dump-output"));
    }

    #[test]
    fn partial_config_uses_safe_defaults() {
        let config: Config = toml::from_str(r##"token = "abc""##).unwrap();
        assert_eq!(config.token, "abc");
        assert_eq!(config.port, 18421);
        assert!(config.dry_run);
    }
}
