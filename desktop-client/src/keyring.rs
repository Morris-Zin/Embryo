use std::error::Error;
use std::fs;
use std::path::Path;

const KEY_BYTES: usize = 32;

fn random_key() -> Result<Vec<u8>, Box<dyn Error + Send + Sync>> {
    let mut key = vec![0u8; KEY_BYTES];
    getrandom::getrandom(&mut key)
        .map_err(|error| format!("operating-system randomness failed: {error}"))?;
    Ok(key)
}

#[cfg(target_os = "windows")]
fn protect_key(key: &[u8]) -> Result<Vec<u8>, Box<dyn Error + Send + Sync>> {
    Ok(windows_dpapi::encrypt_data(
        key,
        windows_dpapi::Scope::User,
        None,
    )?)
}

#[cfg(target_os = "windows")]
fn unprotect_key(blob: &[u8]) -> Result<Vec<u8>, Box<dyn Error + Send + Sync>> {
    Ok(windows_dpapi::decrypt_data(
        blob,
        windows_dpapi::Scope::User,
        None,
    )?)
}

#[cfg(not(target_os = "windows"))]
fn protect_key(key: &[u8]) -> Result<Vec<u8>, Box<dyn Error + Send + Sync>> {
    // Linux is only the development target. Windows shop computers use DPAPI above.
    Ok(key.to_vec())
}

#[cfg(not(target_os = "windows"))]
fn unprotect_key(blob: &[u8]) -> Result<Vec<u8>, Box<dyn Error + Send + Sync>> {
    Ok(blob.to_vec())
}

pub fn load_or_create_key(path: &Path) -> Result<Vec<u8>, Box<dyn Error + Send + Sync>> {
    if let Ok(blob) = fs::read(path) {
        let key = unprotect_key(&blob)?;
        if key.len() == KEY_BYTES {
            return Ok(key);
        }
        return Err(format!("invalid catalog key length in {}", path.display()).into());
    }

    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let key = random_key()?;
    let blob = protect_key(&key)?;
    fs::write(path, blob)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o600))?;
    }
    Ok(key)
}

pub fn hex_key(key: &[u8]) -> String {
    let mut output = String::with_capacity(key.len() * 2);
    for byte in key {
        output.push_str(&format!("{byte:02x}"));
    }
    output
}
