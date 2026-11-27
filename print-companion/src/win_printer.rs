use std::error::Error;
use std::fmt;
use std::fs;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

#[derive(Debug, Clone)]
pub struct PrintError {
    message: String,
}

impl PrintError {
    pub fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
        }
    }
}

impl fmt::Display for PrintError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.message)
    }
}

impl Error for PrintError {}

impl From<std::io::Error> for PrintError {
    fn from(err: std::io::Error) -> Self {
        Self::new(err.to_string())
    }
}

pub trait PrinterBackend: Send + Sync {
    fn is_dry_run(&self) -> bool;
    fn list_printers(&self) -> Result<Vec<String>, PrintError>;
    fn print_voucher(
        &self,
        printer_name: &str,
        voucher_no: &str,
        escpos: &[u8],
        preview: &str,
    ) -> Result<Vec<PathBuf>, PrintError>;
    fn print_a4(
        &self,
        printer_name: &str,
        voucher_no: &str,
        html: &str,
    ) -> Result<Vec<PathBuf>, PrintError>;
}

#[derive(Debug, Clone)]
pub struct DryRunBackend {
    dump_dir: PathBuf,
}

impl DryRunBackend {
    pub fn new(dump_dir: impl Into<PathBuf>) -> Self {
        Self {
            dump_dir: dump_dir.into(),
        }
    }
}

impl PrinterBackend for DryRunBackend {
    fn is_dry_run(&self) -> bool {
        true
    }

    fn list_printers(&self) -> Result<Vec<String>, PrintError> {
        Ok(vec![
            "DRY-RUN Voucher Printer".to_string(),
            "DRY-RUN A4 Printer".to_string(),
        ])
    }

    fn print_voucher(
        &self,
        _printer_name: &str,
        voucher_no: &str,
        escpos: &[u8],
        preview: &str,
    ) -> Result<Vec<PathBuf>, PrintError> {
        fs::create_dir_all(&self.dump_dir)?;
        let stem = artifact_stem(voucher_no);
        let bin_path = self.dump_dir.join(format!("{stem}.escpos.bin"));
        let txt_path = self.dump_dir.join(format!("{stem}.txt"));
        fs::write(&bin_path, escpos)?;
        fs::write(&txt_path, preview)?;
        Ok(vec![bin_path, txt_path])
    }

    fn print_a4(
        &self,
        _printer_name: &str,
        voucher_no: &str,
        html: &str,
    ) -> Result<Vec<PathBuf>, PrintError> {
        fs::create_dir_all(&self.dump_dir)?;
        let stem = artifact_stem(voucher_no);
        let path = self.dump_dir.join(format!("{stem}.a4.html"));
        fs::write(&path, html)?;
        Ok(vec![path])
    }
}

pub fn create_backend(dry_run: bool, dump_dir: &Path) -> Box<dyn PrinterBackend> {
    #[cfg(target_os = "windows")]
    {
        if !dry_run {
            return Box::new(WindowsPrinterBackend::new());
        }
    }

    let _ = dry_run;
    Box::new(DryRunBackend::new(dump_dir.to_path_buf()))
}

fn artifact_stem(voucher_no: &str) -> String {
    let safe_voucher: String = voucher_no
        .chars()
        .map(|ch| {
            if ch.is_ascii_alphanumeric() || ch == '-' || ch == '_' {
                ch
            } else {
                '_'
            }
        })
        .collect();
    format!(
        "{}-{}-{}",
        timestamp_nanos(),
        std::process::id(),
        if safe_voucher.is_empty() {
            "receipt".to_string()
        } else {
            safe_voucher
        }
    )
}

fn timestamp_nanos() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or(0)
}

#[cfg(target_os = "windows")]
pub struct WindowsPrinterBackend;

#[cfg(target_os = "windows")]
impl WindowsPrinterBackend {
    pub fn new() -> Self {
        Self
    }
}

#[cfg(target_os = "windows")]
impl PrinterBackend for WindowsPrinterBackend {
    fn is_dry_run(&self) -> bool {
        false
    }

    fn list_printers(&self) -> Result<Vec<String>, PrintError> {
        match windows_impl::list_printers_winspool() {
            Ok(printers) if !printers.is_empty() => Ok(printers),
            _ => windows_impl::list_printers_powershell(),
        }
    }

    fn print_voucher(
        &self,
        printer_name: &str,
        _voucher_no: &str,
        escpos: &[u8],
        _preview: &str,
    ) -> Result<Vec<PathBuf>, PrintError> {
        windows_impl::raw_print(printer_name, escpos)?;
        Ok(Vec::new())
    }

    fn print_a4(
        &self,
        printer_name: &str,
        voucher_no: &str,
        html: &str,
    ) -> Result<Vec<PathBuf>, PrintError> {
        windows_impl::print_a4_html(printer_name, voucher_no, html)?;
        Ok(Vec::new())
    }
}

#[cfg(target_os = "windows")]
mod windows_impl {
    use super::PrintError;
    use std::env;
    use std::ffi::c_void;
    use std::fs;
    use std::path::PathBuf;
    use std::process::Command;
    use std::ptr;
    use std::slice;
    use std::time::{SystemTime, UNIX_EPOCH};
    use windows_sys::Win32::Foundation::HANDLE;
    use windows_sys::Win32::Graphics::Printing::{
        ClosePrinter, EnumPrintersW, EndDocPrinter, EndPagePrinter, GetDefaultPrinterW,
        OpenPrinterW, StartDocPrinterW, StartPagePrinter, WritePrinter, DOC_INFO_1W,
        PRINTER_ENUM_CONNECTIONS, PRINTER_ENUM_LOCAL, PRINTER_INFO_2W,
    };

    struct PrinterHandle(HANDLE);

    impl Drop for PrinterHandle {
        fn drop(&mut self) {
            unsafe {
                ClosePrinter(self.0);
            }
        }
    }

    pub fn raw_print(printer_name: &str, bytes: &[u8]) -> Result<(), PrintError> {
        let resolved_name = resolve_printer_name(printer_name)?;
        let printer_wide = wide(&resolved_name);
        let mut handle = ptr::null_mut();
        let opened = unsafe { OpenPrinterW(printer_wide.as_ptr(), &mut handle, ptr::null()) };
        if opened == 0 {
            return Err(last_error(format!("OpenPrinterW failed for '{resolved_name}'")));
        }
        let _handle_guard = PrinterHandle(handle);

        let doc_name = wide("Khitmee Sales Voucher");
        let datatype = wide("RAW");
        let doc_info = DOC_INFO_1W {
            pDocName: doc_name.as_ptr() as *mut u16,
            pOutputFile: ptr::null_mut(),
            pDatatype: datatype.as_ptr() as *mut u16,
        };

        let job = unsafe { StartDocPrinterW(handle, 1, &doc_info) };
        if job == 0 {
            return Err(last_error("StartDocPrinterW failed"));
        }

        let mut write_error = None;
        if unsafe { StartPagePrinter(handle) } == 0 {
            write_error = Some(last_error("StartPagePrinter failed"));
        } else {
            let mut offset = 0usize;
            while offset < bytes.len() && write_error.is_none() {
                let remaining = bytes.len() - offset;
                let chunk_len = remaining.min(u32::MAX as usize) as u32;
                let mut written = 0u32;
                let ok = unsafe {
                    WritePrinter(
                        handle,
                        bytes.as_ptr().add(offset) as *const c_void,
                        chunk_len,
                        &mut written,
                    )
                };
                if ok == 0 {
                    write_error = Some(last_error("WritePrinter failed"));
                } else if written == 0 {
                    write_error = Some(PrintError::new("WritePrinter wrote zero bytes"));
                } else {
                    offset += written as usize;
                }
            }
            unsafe {
                EndPagePrinter(handle);
            }
        }

        if unsafe { EndDocPrinter(handle) } == 0 && write_error.is_none() {
            write_error = Some(last_error("EndDocPrinter failed"));
        }

        if let Some(err) = write_error {
            Err(err)
        } else {
            Ok(())
        }
    }

    pub fn list_printers_winspool() -> Result<Vec<String>, PrintError> {
        let flags = PRINTER_ENUM_LOCAL | PRINTER_ENUM_CONNECTIONS;
        let mut needed = 0u32;
        let mut returned = 0u32;
        unsafe {
            EnumPrintersW(
                flags,
                ptr::null(),
                2,
                ptr::null_mut(),
                0,
                &mut needed,
                &mut returned,
            );
        }
        if needed == 0 {
            return Ok(Vec::new());
        }

        let mut backing = vec![0u64; (needed as usize + 7) / 8];
        let ok = unsafe {
            EnumPrintersW(
                flags,
                ptr::null(),
                2,
                backing.as_mut_ptr() as *mut u8,
                needed,
                &mut needed,
                &mut returned,
            )
        };
        if ok == 0 {
            return Err(last_error("EnumPrintersW failed"));
        }

        let infos = unsafe {
            slice::from_raw_parts(backing.as_ptr() as *const PRINTER_INFO_2W, returned as usize)
        };
        Ok(infos
            .iter()
            .filter_map(|info| unsafe { wide_string(info.pPrinterName) })
            .filter(|name| !name.is_empty())
            .collect())
    }

    pub fn list_printers_powershell() -> Result<Vec<String>, PrintError> {
        let output = Command::new("powershell.exe")
            .args([
                "-NoProfile",
                "-Command",
                "Get-Printer | Select-Object -ExpandProperty Name",
            ])
            .output()
            .map_err(|err| PrintError::new(format!("failed to run PowerShell printer query: {err}")))?;
        if !output.status.success() {
            return Err(PrintError::new(format!(
                "PowerShell printer query failed: {}",
                String::from_utf8_lossy(&output.stderr).trim()
            )));
        }
        Ok(String::from_utf8_lossy(&output.stdout)
            .lines()
            .map(str::trim)
            .filter(|line| !line.is_empty())
            .map(ToString::to_string)
            .collect())
    }

    pub fn print_a4_html(
        printer_name: &str,
        voucher_no: &str,
        html: &str,
    ) -> Result<(), PrintError> {
        let stem = format!(
            "khitmee-a4-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map(|duration| duration.as_nanos())
                .unwrap_or(0)
        );
        let safe_voucher: String = voucher_no
            .chars()
            .map(|ch| if ch.is_ascii_alphanumeric() || ch == '-' || ch == '_' { ch } else { '_' })
            .collect();
        let html_path = env::temp_dir().join(format!("{stem}-{safe_voucher}.html"));
        let pdf_path = env::temp_dir().join(format!("{stem}-{safe_voucher}.pdf"));
        fs::write(&html_path, html)?;

        let mut pdf_ready = false;
        if let Some(browser) = find_browser() {
            let status = Command::new(browser)
                .arg("--headless")
                .arg("--disable-gpu")
                .arg("--no-first-run")
                .arg("--no-pdf-header-footer")
                .arg(format!("--print-to-pdf={}", pdf_path.display()))
                .arg(file_url(&html_path))
                .status();
            pdf_ready = matches!(status, Ok(status) if status.success()) && pdf_path.exists();
        }

        if pdf_ready {
            if let Some(sumatra) = find_sumatra() {
                let mut command = Command::new(sumatra);
                if printer_name.trim().is_empty() {
                    command.arg("-print-to-default");
                } else {
                    command.arg("-print-to").arg(printer_name);
                }
                command.arg("-silent").arg(&pdf_path);
                let status = command.status();
                if matches!(status, Ok(status) if status.success()) {
                    return Ok(());
                }
            }
        }

        let uses_default = printer_name.trim().is_empty()
            || default_printer()
                .map(|default| default.eq_ignore_ascii_case(printer_name.trim()))
                .unwrap_or(false);
        if uses_default {
            return powershell_print_html(&html_path);
        }

        Err(PrintError::new(
            "A4 printing to a named printer needs SumatraPDF plus Edge/Chrome headless printing, or set that printer as the Windows default",
        ))
    }

    fn resolve_printer_name(printer_name: &str) -> Result<String, PrintError> {
        if printer_name.trim().is_empty() {
            default_printer()
        } else {
            Ok(printer_name.trim().to_string())
        }
    }

    fn default_printer() -> Result<String, PrintError> {
        let mut len = 0u32;
        unsafe {
            GetDefaultPrinterW(ptr::null_mut(), &mut len);
        }
        if len == 0 {
            return Err(PrintError::new("no default Windows printer is configured"));
        }
        let mut buffer = vec![0u16; len as usize];
        if unsafe { GetDefaultPrinterW(buffer.as_mut_ptr(), &mut len) } == 0 {
            return Err(last_error("GetDefaultPrinterW failed"));
        }
        let end = buffer.iter().position(|ch| *ch == 0).unwrap_or(buffer.len());
        Ok(String::from_utf16_lossy(&buffer[..end]))
    }

    fn powershell_print_html(path: &std::path::Path) -> Result<(), PrintError> {
        let path_text = path.to_string_lossy().replace('\'', "''");
        let script = format!(
            "Start-Process -FilePath '{}' -Verb Print -WindowStyle Hidden",
            path_text
        );
        let output = Command::new("powershell.exe")
            .args(["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", &script])
            .output()
            .map_err(|err| PrintError::new(format!("failed to start PowerShell print helper: {err}")))?;
        if output.status.success() {
            Ok(())
        } else {
            Err(PrintError::new(format!(
                "PowerShell print helper failed: {}",
                String::from_utf8_lossy(&output.stderr).trim()
            )))
        }
    }

    fn find_browser() -> Option<PathBuf> {
        let program_files_x86 = env::var("ProgramFiles(x86)")
            .unwrap_or_else(|_| r"C:\Program Files (x86)".to_string());
        let program_files = env::var("ProgramFiles").unwrap_or_else(|_| r"C:\Program Files".to_string());
        [
            PathBuf::from(&program_files_x86).join(r"Microsoft\Edge\Application\msedge.exe"),
            PathBuf::from(&program_files).join(r"Microsoft\Edge\Application\msedge.exe"),
            PathBuf::from(&program_files).join(r"Google\Chrome\Application\chrome.exe"),
            PathBuf::from(&program_files_x86).join(r"Google\Chrome\Application\chrome.exe"),
        ]
        .into_iter()
        .find(|path| path.exists())
    }

    fn find_sumatra() -> Option<PathBuf> {
        let mut candidates = Vec::new();
        if let Ok(local_app_data) = env::var("LOCALAPPDATA") {
            candidates.push(PathBuf::from(local_app_data).join(r"SumatraPDF\SumatraPDF.exe"));
        }
        if let Ok(program_files) = env::var("ProgramFiles") {
            candidates.push(PathBuf::from(program_files).join(r"SumatraPDF\SumatraPDF.exe"));
        }
        if let Ok(program_files_x86) = env::var("ProgramFiles(x86)") {
            candidates.push(PathBuf::from(program_files_x86).join(r"SumatraPDF\SumatraPDF.exe"));
        }
        candidates.into_iter().find(|path| path.exists())
    }

    fn file_url(path: &std::path::Path) -> String {
        let raw = path.to_string_lossy().replace('\\', "/");
        let mut encoded = String::new();
        for byte in raw.bytes() {
            match byte {
                b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'/' | b':' | b'-' | b'_' | b'.' => {
                    encoded.push(byte as char)
                }
                _ => encoded.push_str(&format!("%{byte:02X}")),
            }
        }
        format!("file:///{encoded}")
    }

    fn wide(text: &str) -> Vec<u16> {
        text.encode_utf16().chain(std::iter::once(0)).collect()
    }

    unsafe fn wide_string(ptr: *mut u16) -> Option<String> {
        if ptr.is_null() {
            return None;
        }
        let mut len = 0usize;
        while unsafe { *ptr.add(len) } != 0 {
            len += 1;
        }
        Some(String::from_utf16_lossy(unsafe {
            slice::from_raw_parts(ptr, len)
        }))
    }

    fn last_error(context: impl Into<String>) -> PrintError {
        PrintError::new(format!(
            "{}: {}",
            context.into(),
            std::io::Error::last_os_error()
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn test_dump_dir(name: &str) -> PathBuf {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("target")
            .join("test-tmp")
            .join(format!("{name}-{}-{nanos}", std::process::id()))
    }

    #[test]
    fn dry_run_lists_safe_fake_printers() {
        let backend = DryRunBackend::new(test_dump_dir("list"));
        let printers = backend.list_printers().unwrap();
        assert_eq!(printers.len(), 2);
        assert!(printers.iter().any(|name| name.contains("Voucher")));
        assert!(printers.iter().any(|name| name.contains("A4")));
    }

    #[test]
    fn dry_run_writes_voucher_binary_and_text_preview() {
        let dir = test_dump_dir("voucher");
        let backend = DryRunBackend::new(&dir);
        let artifacts = backend
            .print_voucher("ignored", "SINV/0001", b"escpos-bytes", "preview text")
            .unwrap();

        assert_eq!(artifacts.len(), 2);
        assert!(artifacts.iter().any(|path| path.file_name().unwrap().to_string_lossy().ends_with("SINV_0001.escpos.bin")));
        assert!(artifacts.iter().any(|path| path.file_name().unwrap().to_string_lossy().ends_with("SINV_0001.txt")));
        assert_eq!(fs::read(&artifacts[0]).unwrap(), b"escpos-bytes");
        assert!(dir.exists());
    }

    #[test]
    fn dry_run_writes_a4_html() {
        let dir = test_dump_dir("a4");
        let backend = DryRunBackend::new(&dir);
        let artifacts = backend
            .print_a4("ignored", "SINV-0002", "<html>test</html>")
            .unwrap();

        assert_eq!(artifacts.len(), 1);
        assert!(artifacts[0].file_name().unwrap().to_string_lossy().ends_with("SINV-0002.a4.html"));
        assert_eq!(fs::read_to_string(&artifacts[0]).unwrap(), "<html>test</html>");
    }
}
