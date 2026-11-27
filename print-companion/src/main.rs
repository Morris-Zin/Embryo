use khitmee_print_companion::config::Config;
use khitmee_print_companion::server::bind_and_serve;
use khitmee_print_companion::win_printer::create_backend;
use std::env;
use std::path::PathBuf;
use std::process;

#[derive(Debug, Default, PartialEq, Eq)]
struct CliOptions {
    config_path: Option<PathBuf>,
    port: Option<u16>,
    dry_run: bool,
    dump_dir: Option<PathBuf>,
    help: bool,
}

fn main() {
    if let Err(err) = run() {
        eprintln!("khitmee-print-companion: {err}");
        process::exit(1);
    }
}

fn run() -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let cli = parse_args(env::args().skip(1))?;
    if cli.help {
        print_usage();
        return Ok(());
    }

    let config_path = cli
        .config_path
        .clone()
        .unwrap_or_else(|| PathBuf::from("config.toml"));
    if cli.config_path.is_some() && !config_path.exists() {
        return Err(format!("config file not found: {}", config_path.display()).into());
    }
    let mut config = Config::load(&config_path)?;
    config.apply_overrides(cli.port, cli.dry_run, cli.dump_dir);

    if config.token.is_empty() {
        eprintln!("warning: token is empty; POST /print will reject every request until config.toml sets token");
    }

    let backend = create_backend(config.dry_run, &config.dump_dir);
    let effective_dry_run = backend.is_dry_run();
    println!(
        "listening on http://127.0.0.1:{} (dry_run={effective_dry_run})",
        config.port
    );
    if effective_dry_run {
        println!("dry-run output directory: {}", config.dump_dir.display());
    }

    bind_and_serve(config, backend)?;
    Ok(())
}

fn parse_args<I>(args: I) -> Result<CliOptions, String>
where
    I: IntoIterator<Item = String>,
{
    let mut options = CliOptions::default();
    let mut args = args.into_iter();

    while let Some(arg) = args.next() {
        if arg == "--help" || arg == "-h" {
            options.help = true;
        } else if arg == "--dry-run" {
            options.dry_run = true;
        } else if arg == "--config" {
            options.config_path = Some(PathBuf::from(required_value("--config", args.next())?));
        } else if let Some(value) = arg.strip_prefix("--config=") {
            options.config_path = Some(PathBuf::from(value));
        } else if arg == "--port" {
            let value = required_value("--port", args.next())?;
            options.port = Some(
                value
                    .parse::<u16>()
                    .map_err(|_| format!("--port must be a number from 0 to 65535, got '{value}'"))?,
            );
        } else if let Some(value) = arg.strip_prefix("--port=") {
            options.port = Some(
                value
                    .parse::<u16>()
                    .map_err(|_| format!("--port must be a number from 0 to 65535, got '{value}'"))?,
            );
        } else if arg == "--dump-dir" {
            options.dump_dir = Some(PathBuf::from(required_value("--dump-dir", args.next())?));
        } else if let Some(value) = arg.strip_prefix("--dump-dir=") {
            options.dump_dir = Some(PathBuf::from(value));
        } else {
            return Err(format!("unknown argument '{arg}'"));
        }
    }

    Ok(options)
}

fn required_value(flag: &str, value: Option<String>) -> Result<String, String> {
    value.ok_or_else(|| format!("{flag} requires a value"))
}

fn print_usage() {
    println!(
        "khitmee-print-companion\n\n\
         Usage: khitmee-print-companion [--config config.toml] [--port 18421] [--dry-run] [--dump-dir print-dump]\n\n\
         Options:\n\
           --config PATH     Load config.toml from PATH (default: config.toml)\n\
           --port PORT       Override configured port (default: 18421)\n\
           --dry-run         Write print output files instead of using physical printers\n\
           --dump-dir PATH   Override dry-run output directory\n\
           -h, --help        Show this help"
    );
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_cli_overrides() {
        let options = parse_args([
            "--config".to_string(),
            "custom.toml".to_string(),
            "--port=18422".to_string(),
            "--dry-run".to_string(),
            "--dump-dir".to_string(),
            "dump".to_string(),
        ])
        .unwrap();

        assert_eq!(options.config_path, Some(PathBuf::from("custom.toml")));
        assert_eq!(options.port, Some(18422));
        assert!(options.dry_run);
        assert_eq!(options.dump_dir, Some(PathBuf::from("dump")));
    }

    #[test]
    fn rejects_unknown_cli_argument() {
        assert!(parse_args(["--unexpected".to_string()]).is_err());
    }

    #[test]
    fn rejects_invalid_port() {
        assert!(parse_args(["--port".to_string(), "not-a-port".to_string()]).is_err());
    }
}
