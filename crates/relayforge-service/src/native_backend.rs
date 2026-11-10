//! Fixed native service modes share the desktop lifecycle without a Python daemon.
use crate::{create_ap, networkmanager, nm_journal::Result};
use relayforge_core::{config::Config, Status};
use serde_json::Value;
use std::sync::Arc;
use tokio::sync::watch;
pub enum Backend {
    NetworkManager(Arc<networkmanager::Backend>),
    CreateAp(Arc<create_ap::Backend>),
}
impl Backend {
    pub async fn environment_preflight(&self, config: &Config, phy: u32) -> Result<()> {
        match self {
            Self::CreateAp(b) => b.environment_preflight(config, phy).await,
            Self::NetworkManager(_) => Ok(()),
        }
    }
    pub fn driver_preflight(&self, config: &Config) -> Result<Option<String>> {
        match self {
            Self::CreateAp(b) => b.driver_preflight(config).map(Some),
            Self::NetworkManager(_) => Ok(None),
        }
    }
    pub async fn bridge_preflight(&self, config: &Config) -> Result<()> {
        match self {
            Self::CreateAp(b) => b.bridge_preflight(config).await,
            Self::NetworkManager(_) => {
                Err("Existing-bridge sharing requires the native create_ap backend")
            }
        }
    }
    pub fn name(&self) -> &'static str {
        match self {
            Self::NetworkManager(_) => "networkmanager",
            Self::CreateAp(_) => "create_ap",
        }
    }
    pub async fn recover(&self) -> Result<()> {
        match self {
            Self::NetworkManager(b) => b.recover().await,
            Self::CreateAp(b) => b.recover().await,
        }
    }
    pub async fn start(
        &self,
        c: &Config,
        cancel: watch::Receiver<u64>,
        expected: Option<&networkmanager::Upstream>,
    ) -> Value {
        match self {
            Self::NetworkManager(b) => b.start(c, cancel, expected).await,
            Self::CreateAp(b) => b.start(c, cancel, expected).await,
        }
    }
    pub async fn stop(&self) -> bool {
        match self {
            Self::NetworkManager(b) => b.stop().await,
            Self::CreateAp(b) => b.stop().await,
        }
    }
    pub async fn poll(&self) -> Option<(Status, Value)> {
        match self {
            Self::NetworkManager(b) => b.poll().await,
            Self::CreateAp(b) => b.poll().await,
        }
    }
}
