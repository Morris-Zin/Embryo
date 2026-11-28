use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::error::Error;
use std::fs;
use std::path::Path;

#[derive(Clone, Copy, Debug, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum MigrationStatus {
    Discovered,
    ReferenceCaptured,
    Blocked,
    Implementing,
    ImplementedPilot,
    Verified,
    RegressionLocked,
    Rejected,
}

impl MigrationStatus {
    pub fn is_complete(self) -> bool {
        matches!(self, Self::Verified | Self::RegressionLocked)
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct EvidenceRef {
    pub kind: String,
    pub path: String,
    #[serde(default)]
    pub sha256: String,
    #[serde(default)]
    pub note: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct DiscoveredControl {
    pub control_type: String,
    pub id: String,
    #[serde(default)]
    pub label: String,
    #[serde(default)]
    pub role: String,
    #[serde(default)]
    pub keyboard: Vec<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ApiContract {
    pub endpoint: String,
    pub authority: String,
    #[serde(default)]
    pub required_permissions: Vec<String>,
    #[serde(default)]
    pub mutation: bool,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct AcceptanceCriteria {
    pub visual_max_failed_pixel_ratio: f64,
    pub require_text_parity: bool,
    pub require_accessibility_parity: bool,
    pub require_keyboard_parity: bool,
    pub require_business_outcome_parity: bool,
    pub max_startup_regression_ratio: f64,
    pub max_idle_memory_bytes: u64,
    pub max_p95_input_latency_ms: f64,
}

impl Default for AcceptanceCriteria {
    fn default() -> Self {
        Self {
            visual_max_failed_pixel_ratio: 0.0,
            require_text_parity: true,
            require_accessibility_parity: true,
            require_keyboard_parity: true,
            require_business_outcome_parity: true,
            max_startup_regression_ratio: 1.0,
            max_idle_memory_bytes: 256 * 1024 * 1024,
            max_p95_input_latency_ms: 50.0,
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ScreenMigration {
    pub id: String,
    pub title: String,
    pub source_route: String,
    pub status: MigrationStatus,
    #[serde(default)]
    pub dependencies: Vec<String>,
    #[serde(default)]
    pub controls: Vec<DiscoveredControl>,
    #[serde(default)]
    pub api_contracts: Vec<ApiContract>,
    #[serde(default)]
    pub native_component: String,
    #[serde(default)]
    pub owner_scope: String,
    #[serde(default)]
    pub evidence: Vec<EvidenceRef>,
    #[serde(default)]
    pub failures: Vec<String>,
    #[serde(default)]
    pub acceptance: AcceptanceCriteria,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct MigrationRegistry {
    pub schema_version: u32,
    pub generated_at: String,
    pub framework: String,
    #[serde(default)]
    pub design_tokens: BTreeMap<String, String>,
    #[serde(default)]
    pub screens: Vec<ScreenMigration>,
    #[serde(default)]
    pub shared_component_scopes: BTreeMap<String, String>,
    #[serde(default)]
    pub experiments: Vec<String>,
}

impl MigrationRegistry {
    pub fn load(path: &Path) -> Result<Self, Box<dyn Error + Send + Sync>> {
        Ok(serde_json::from_str(&fs::read_to_string(path)?)?)
    }

    pub fn save(&self, path: &Path) -> Result<(), Box<dyn Error + Send + Sync>> {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)?;
        }
        fs::write(path, serde_json::to_string_pretty(self)? + "\n")?;
        Ok(())
    }

    pub fn screen(&self, id: &str) -> Option<&ScreenMigration> {
        self.screens.iter().find(|screen| screen.id == id)
    }

    pub fn regression_locked(&self) -> impl Iterator<Item = &ScreenMigration> {
        self.screens
            .iter()
            .filter(|screen| screen.status == MigrationStatus::RegressionLocked)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn strict_default_never_weakens_visual_or_business_parity() {
        let criteria = AcceptanceCriteria::default();
        assert_eq!(criteria.visual_max_failed_pixel_ratio, 0.0);
        assert!(criteria.require_text_parity);
        assert!(criteria.require_accessibility_parity);
        assert!(criteria.require_keyboard_parity);
        assert!(criteria.require_business_outcome_parity);
    }

    #[test]
    fn registry_roundtrip_preserves_screen_state() {
        let registry = MigrationRegistry {
            schema_version: 1,
            generated_at: "2026-11-27T00:00:00Z".into(),
            framework: "slint".into(),
            design_tokens: BTreeMap::new(),
            screens: vec![ScreenMigration {
                id: "sales-voucher".into(),
                title: "Sales Counter".into(),
                source_route: "/khitmee_sales".into(),
                status: MigrationStatus::ImplementedPilot,
                dependencies: vec![],
                controls: vec![],
                api_contracts: vec![],
                native_component: "SalesVoucher".into(),
                owner_scope: "screens/sales-voucher".into(),
                evidence: vec![],
                failures: vec![],
                acceptance: AcceptanceCriteria::default(),
            }],
            shared_component_scopes: BTreeMap::new(),
            experiments: vec![],
        };
        let text = serde_json::to_string(&registry).unwrap();
        let parsed: MigrationRegistry = serde_json::from_str(&text).unwrap();
        assert_eq!(
            parsed.screen("sales-voucher").unwrap().status,
            MigrationStatus::ImplementedPilot
        );
    }
}
