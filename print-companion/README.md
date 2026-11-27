# Khitmee Print Companion

A tiny Rust HTTP server that lets the browser-based Khitmee ERPNext app print sales vouchers on a shop Windows PC.

It listens only on `http://127.0.0.1:18421`. Other computers cannot connect to it. Browser access is limited by an explicit CORS origin allowlist, and printing requires the `X-Khitmee-Print-Token` header.

## What it prints

- `voucher`: raw ESC/POS receipt bytes for a thermal printer, with 80mm-style 48-column layout by default. Set `paper_width_chars = 32` for many 58mm printers.
- `a4`: a self-contained A4 HTML voucher with table, totals, and signature lines. No network assets are used.

In `dry_run = true` mode no physical printer is touched. Output files are written to `dump_dir`:

- `<voucher>.escpos.bin` — exact bytes that would be sent to the thermal printer
- `<voucher>.txt` — readable receipt preview
- `<voucher>.a4.html` — A4 HTML document

## HTTP API

### `GET /health`

Response:

```json
{
  "status": "ok",
  "version": "0.1.0",
  "dry_run": true,
  "voucher_printer": "POS-80",
  "a4_printer": "Canon A4"
}
```

### `GET /printers`

Response:

```json
{"printers": ["POS-80", "Canon A4"]}
```

### `POST /print`

Required header:

```text
X-Khitmee-Print-Token: your-configured-token
```

Body schema:

```json
{
  "printer_type": "voucher",
  "voucher_no": "SINV-0001",
  "title": "Khit Me Sales Voucher",
  "shop_name": "Dist Hub 1",
  "date": "2026-11-26 16:00:00",
  "party": "Walk-in Customer",
  "lines": [
    {"name": "Product Sample A", "qty": 5, "rate": 11760, "amount": 58800}
  ],
  "total": 58800,
  "footer": "Thank you!"
}
```

Use `"printer_type": "a4"` for the A4 document.

## Student setup on a Windows shop PC

### 1. Install Rust

Open PowerShell and run:

```powershell
winget install Rustlang.Rustup
```

Close and reopen PowerShell, then check:

```powershell
cargo --version
rustc --version
```

If `winget` is unavailable, install Rust from the official rustup site: <https://rustup.rs/>.

### 2. Build the companion

Open PowerShell in this directory:

```powershell
cd E:\Khitmee-ERPNext\print-companion
cargo build --locked --release
```

The executable will be:

```text
E:\Khitmee-ERPNext\print-companion\target\release\khitmee-print-companion.exe
```

The release profile is size-optimized and uses only a few small Rust crates.

### 3. Find printer names

Run:

```powershell
Get-Printer | Select-Object -ExpandProperty Name
```

Copy the exact names. Examples only — your names will differ:

```text
POS-80
Canon G3010 series
```

You can also look in **Control Panel → Devices and Printers**.

### 4. Create the real config

Copy the example:

```powershell
Copy-Item .\config.example.toml .\config.toml
```

Edit `config.toml` in Notepad:

```powershell
notepad .\config.toml
```

Set these values:

```toml
token = "replace-with-a-long-random-token"
voucher_printer_name = "POS-80"
a4_printer_name = "Canon G3010 series"
paper_width_chars = 48
dry_run = true
```

Important:

- Never commit or share the real token.
- If a printer name is empty, the Windows default printer is used where possible.
- Use `paper_width_chars = 48` for typical 80mm thermal printers.
- Use `paper_width_chars = 32` for typical 58mm thermal printers.
- Add the production ERP origin to `allowed_origins` when needed, for example `"https://erp.example.com"`.

### 5. Test without touching physical printers

Keep `dry_run = true`, then run:

```powershell
.\target\release\khitmee-print-companion.exe --config .\config.toml
```

In another PowerShell window, check health:

```powershell
Invoke-RestMethod http://127.0.0.1:18421/health
```

Check visible printers:

```powershell
Invoke-RestMethod http://127.0.0.1:18421/printers
```

In dry-run mode the printer list contains safe fake names. That is expected.

Send a test voucher. Replace `your-token` with the token from `config.toml`:

```powershell
$headers = @{ "X-Khitmee-Print-Token" = "your-token" }
$body = @{
  printer_type = "voucher"
  voucher_no = "SINV-0001"
  title = "Khit Me Sales Voucher"
  shop_name = "Dist Hub 1"
  date = "2026-11-26 16:00:00"
  party = "Walk-in Customer"
  lines = @(
    @{ name = "Product Sample A"; qty = 5; rate = 11760; amount = 58800 }
  )
  total = 58800
  footer = "Thank you!"
} | ConvertTo-Json -Depth 5

Invoke-RestMethod -Method Post -Uri http://127.0.0.1:18421/print -Headers $headers -Body $body -ContentType "application/json"
```

Open the `print-dump` folder and inspect the `.txt` preview and `.escpos.bin` output.

### 6. Test physical printing

After dry-run output looks correct:

1. Stop the companion with `Ctrl+C`.
2. Set `dry_run = false` in `config.toml`.
3. Start it again:

```powershell
.\target\release\khitmee-print-companion.exe --config .\config.toml
```

4. Send the same test print again.

Thermal voucher printing uses the Windows spooler in `RAW` mode. A4 printing writes a temporary self-contained HTML file. For reliable printing to a named non-default A4 printer, install SumatraPDF and have Edge or Chrome available; the companion uses the browser's headless PDF rendering and SumatraPDF's named-printer print mode. If the configured A4 printer is already the Windows default, the companion can fall back to the Windows print verb.

### 7. Run automatically when Windows starts

Simple startup-folder method:

1. Press `Win+R`.
2. Type `shell:startup` and press Enter.
3. Right-click in the folder → **New → Shortcut**.
4. Use this target, adjusting paths if needed:

```text
E:\Khitmee-ERPNext\print-companion\target\release\khitmee-print-companion.exe --config E:\Khitmee-ERPNext\print-companion\config.toml
```

5. Name it `Khitmee Print Companion`.
6. Sign out/in or restart Windows, then check:

```powershell
Invoke-RestMethod http://127.0.0.1:18421/health
```

## CLI overrides

```text
--config PATH     Load config.toml from PATH
--port PORT       Override configured port
--dry-run         Force dry-run mode
--dump-dir PATH   Override dry-run output directory
-h, --help        Show help
```

Examples:

```powershell
.\target\release\khitmee-print-companion.exe --config .\config.toml --dry-run
.\target\release\khitmee-print-companion.exe --port 18421 --dump-dir .\print-dump
```

## Development checks

```powershell
cargo fmt --check
cargo check --locked
cargo test --locked
```

## Security notes

- The server binds only to loopback (`127.0.0.1`).
- `POST /print` always requires the configured token.
- Browser origins are exact-match allowlisted. The defaults are only `http://127.0.0.1:18085` and `http://127.0.0.1:18086`.
- Do not print or log the token. Do not commit `config.toml`.
- ESC/POS output uses printer-safe ASCII. Non-ASCII text is replaced or transliterated because many low-cost thermal printers do not implement UTF-8 correctly.
