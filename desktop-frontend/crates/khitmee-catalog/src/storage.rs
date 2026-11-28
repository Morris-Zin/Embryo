use crate::models::CatalogItem;
use rayon::prelude::*;
use rusqlite::{params, Connection};
use std::error::Error;
use std::path::Path;
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

fn unix_seconds() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs() as i64)
        .unwrap_or(0)
}

pub struct EncryptedStore {
    connection: Mutex<Connection>,
}

impl EncryptedStore {
    pub fn open(path: &Path, hex_key: &str) -> Result<Self, Box<dyn Error + Send + Sync>> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let connection = Connection::open(path)?;
        connection.execute_batch(&format!("PRAGMA key = \"x'{hex_key}'\";"))?;
        connection.execute_batch(
            "PRAGMA journal_mode = WAL;
             PRAGMA synchronous = NORMAL;
             CREATE TABLE IF NOT EXISTS catalog_rows (
               scope TEXT NOT NULL,
               item_code TEXT NOT NULL,
               payload TEXT NOT NULL,
               updated_at INTEGER NOT NULL,
               PRIMARY KEY (scope, item_code)
             );
             CREATE TABLE IF NOT EXISTS sync_states (
               scope TEXT PRIMARY KEY,
               sync_token TEXT NOT NULL,
               updated_at INTEGER NOT NULL
             );",
        )?;
        Ok(Self {
            connection: Mutex::new(connection),
        })
    }

    pub fn sync_token(&self, scope: &str) -> Result<String, Box<dyn Error + Send + Sync>> {
        let connection = self.connection.lock().map_err(|_| "store lock poisoned")?;
        let mut statement =
            connection.prepare("SELECT sync_token FROM sync_states WHERE scope = ?1")?;
        let mut rows = statement.query(params![scope])?;
        if let Some(row) = rows.next()? {
            return Ok(row.get::<_, String>(0)?);
        }
        Ok(String::new())
    }

    pub fn item_count(&self, scope: &str) -> Result<usize, Box<dyn Error + Send + Sync>> {
        let connection = self.connection.lock().map_err(|_| "store lock poisoned")?;
        let count: i64 = connection.query_row(
            "SELECT COUNT(*) FROM catalog_rows WHERE scope = ?1",
            params![scope],
            |row| row.get(0),
        )?;
        Ok(count as usize)
    }

    pub fn apply_sync(
        &self,
        scope: &str,
        reset: bool,
        sync_token: &str,
        items: &[CatalogItem],
    ) -> Result<usize, Box<dyn Error + Send + Sync>> {
        let mut connection = self.connection.lock().map_err(|_| "store lock poisoned")?;
        let transaction = connection.transaction()?;
        let now = unix_seconds();
        if reset {
            transaction.execute("DELETE FROM catalog_rows WHERE scope = ?1", params![scope])?;
        }
        for item in items {
            if item.item_code.is_empty() || item.item_code.len() > 140 {
                continue;
            }
            if item.deleted {
                transaction.execute(
                    "DELETE FROM catalog_rows WHERE scope = ?1 AND item_code = ?2",
                    params![scope, item.item_code],
                )?;
            } else {
                let payload = serde_json::to_string(item)?;
                transaction.execute(
                    "INSERT INTO catalog_rows (scope, item_code, payload, updated_at)
                     VALUES (?1, ?2, ?3, ?4)
                     ON CONFLICT(scope, item_code) DO UPDATE SET
                       payload = excluded.payload,
                       updated_at = excluded.updated_at",
                    params![scope, item.item_code, payload, now],
                )?;
            }
        }
        transaction.execute(
            "INSERT INTO sync_states (scope, sync_token, updated_at)
             VALUES (?1, ?2, ?3)
             ON CONFLICT(scope) DO UPDATE SET
               sync_token = excluded.sync_token,
               updated_at = excluded.updated_at",
            params![scope, sync_token, now],
        )?;
        transaction.commit()?;
        drop(connection);
        self.item_count(scope)
    }

    pub fn rows(&self, scope: &str) -> Result<Vec<CatalogItem>, Box<dyn Error + Send + Sync>> {
        let connection = self.connection.lock().map_err(|_| "store lock poisoned")?;
        let mut statement = connection
            .prepare("SELECT payload FROM catalog_rows WHERE scope = ?1 ORDER BY item_code")?;
        let rows = statement.query_map(params![scope], |row| row.get::<_, String>(0))?;
        let payloads = rows.collect::<Result<Vec<String>, rusqlite::Error>>()?;
        payloads
            .into_par_iter()
            .map(|payload| serde_json::from_str::<CatalogItem>(&payload).map_err(Into::into))
            .collect::<Result<Vec<CatalogItem>, Box<dyn Error + Send + Sync>>>()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::keyring::hex_key;

    fn temp_path(name: &str) -> std::path::PathBuf {
        std::env::temp_dir().join(format!("khitmee-store-test-{name}-{}", std::process::id()))
    }

    fn item(code: &str, deleted: bool) -> CatalogItem {
        CatalogItem {
            item_code: code.to_string(),
            item_name: Some(code.to_string()),
            stock_uom: Some("Nos".to_string()),
            item_group: Some("Group".to_string()),
            brand: Some("Brand".to_string()),
            barcode: Some(code.to_string()),
            barcode_text: Some(code.to_string()),
            rate: 2.0,
            actual_qty: 3.0,
            modified: None,
            deleted,
        }
    }

    #[test]
    fn wrong_sqlcipher_key_cannot_open_existing_catalog() {
        let path = temp_path("wrong-key.db");
        let _ = std::fs::remove_file(&path);
        let store = EncryptedStore::open(&path, &hex_key(&[7u8; 32])).unwrap();
        store
            .apply_sync("scope", true, "token", &[item("A", false)])
            .unwrap();
        drop(store);
        let wrong_key = EncryptedStore::open(&path, &hex_key(&[8u8; 32]));
        assert!(
            wrong_key.is_err(),
            "SQLCipher must reject a wrong catalog key"
        );
        let _ = std::fs::remove_file(&path);
        let _ = std::fs::remove_file(path.with_extension("db-shm"));
        let _ = std::fs::remove_file(path.with_extension("db-wal"));
    }

    #[test]
    fn encrypted_roundtrip_upsert_and_delete() {
        let path = temp_path("roundtrip.db");
        let _ = std::fs::remove_file(&path);
        let key = hex_key(&[7u8; 32]);
        let store = EncryptedStore::open(&path, &key).unwrap();
        store
            .apply_sync(
                "scope",
                true,
                "token-1",
                &[item("A", false), item("B", false)],
            )
            .unwrap();
        assert_eq!(store.sync_token("scope").unwrap(), "token-1");
        assert_eq!(store.item_count("scope").unwrap(), 2);
        store
            .apply_sync("scope", false, "token-2", &[item("A", true)])
            .unwrap();
        assert_eq!(store.item_count("scope").unwrap(), 1);
        let rows = store.rows("scope").unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].item_code, "B");
        let _ = std::fs::remove_file(&path);
        let _ = std::fs::remove_file(path.with_extension("db-shm"));
        let _ = std::fs::remove_file(path.with_extension("db-wal"));
    }
}
