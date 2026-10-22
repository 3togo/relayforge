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
    pub async fn start(&self, c: &Config, cancel: watch::Receiver<u64>) -> Value {
        match self {
            Self::NetworkManager(b) => b.start(c, cancel).await,
            Self::CreateAp(b) => b.start(c, cancel).await,
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
