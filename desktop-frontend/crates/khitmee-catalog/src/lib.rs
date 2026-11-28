//! Reusable Khitmee catalog primitives extracted from the PR #8 experiment.
//!
//! This crate intentionally exposes storage and search as an in-process library. The
//! native desktop application must not use the PR #8 loopback HTTP service or Tauri.

pub mod index;
pub mod keyring;
pub mod models;
#[cfg(feature = "sqlcipher")]
pub mod storage;

pub use index::CatalogIndex;
pub use keyring::{hex_key, load_or_create_key};
pub use models::{CatalogItem, SyncPayload};
#[cfg(feature = "sqlcipher")]
pub use storage::EncryptedStore;
