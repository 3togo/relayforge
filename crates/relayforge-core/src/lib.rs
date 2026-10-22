//! Desktop-independent lifecycle and compatibility protocol. No I/O or credentials in state.
pub mod config;
pub mod radio;
pub mod subnet;

use serde::{Deserialize, Serialize};
use serde_json::Value;

pub const BUS_NAME: &str = "io.github.erhanzeyrek.WifiHotspot";
pub const OBJECT_PATH: &str = "/io/github/erhanzeyrek/WifiHotspot";
pub const MANAGE_ACTION: &str = "io.github.erhanzeyrek.WifiHotspot.manage";
pub const MAX_MESSAGE: usize = 1024 * 1024;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Method {
    Start,
    Stop,
    GetStatus,
    GetClients,
    GetConfig,
    SetConfig,
    GetInterfaces,
    GetCapabilities,
    SwitchBandAndReconnect,
    PrepareFirewall,
}

impl Method {
    pub fn protected(self) -> bool {
        matches!(
            self,
            Self::Start
                | Self::Stop
                | Self::GetConfig
                | Self::SetConfig
                | Self::SwitchBandAndReconnect
                | Self::PrepareFirewall
        )
    }
    pub fn name(self) -> &'static str {
        match self {
            Self::Start => "Start",
            Self::Stop => "Stop",
            Self::GetStatus => "GetStatus",
            Self::GetClients => "GetClients",
            Self::GetConfig => "GetConfig",
            Self::SetConfig => "SetConfig",
            Self::GetInterfaces => "GetInterfaces",
            Self::GetCapabilities => "GetCapabilities",
            Self::SwitchBandAndReconnect => "SwitchBandAndReconnect",
            Self::PrepareFirewall => "PrepareFirewall",
        }
    }
}

#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
pub struct Status {
    pub active: bool,
    #[serde(default)]
    pub desired_active: bool,
    #[serde(default)]
    pub state: String,
    #[serde(default)]
    pub client_count: u32,
    #[serde(default)]
    pub iface: String,
    #[serde(default)]
    pub phy_iface: String,
    #[serde(default)]
    pub ssid: String,
    #[serde(default)]
    pub band: String,
    #[serde(default)]
    pub backend: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

#[derive(Default)]
pub struct Lifecycle {
    pub status: Status,
    stop_requested: bool,
    failed: bool,
    epoch: u64,
}

impl Lifecycle {
    pub fn begin(&mut self, method: Method) -> Result<u64, &'static str> {
        if self.failed {
            return Err("Backend unavailable; restart the service after inspecting cleanup.");
        }
        match method {
            Method::Start | Method::SwitchBandAndReconnect => {
                if self.stop_requested
                    || matches!(self.status.state.as_str(), "connecting" | "stopping")
                {
                    return Err("Another hotspot operation is in progress.");
                }
                self.status.desired_active = true;
                self.epoch += 1;
                self.status.state = "connecting".into();
                self.status.error = None;
            }
            Method::Stop => {
                self.stop_requested = true;
                self.epoch += 1;
                self.status.desired_active = false;
                self.status.state = "stopping".into();
            }
            _ => {}
        }
        Ok(self.epoch)
    }

    pub fn epoch(&self) -> u64 {
        self.epoch
    }

    pub fn is_current(&self, epoch: u64) -> bool {
        self.epoch == epoch
    }

    pub fn complete(&mut self, method: Method, epoch: u64, success: bool) {
        if self.is_current(epoch) {
            self.finish(method, success);
        }
    }

    pub fn observe(&mut self, value: Value) -> Result<(), &'static str> {
        let mut status: Status =
            serde_json::from_value(value).map_err(|_| "Invalid backend status")?;
        if self.failed {
            return Ok(());
        }
        if self.stop_requested {
            // A delayed startup notification cannot undo the user's Stop intent.
            status.desired_active = false;
            if self.status.state == "failed" {
                status.state = "failed".into();
                status.error = self.status.error.clone().or(status.error);
            } else {
                status.state = "stopping".into();
            }
        } else if status.state.is_empty() {
            status.state = if status.active { "active" } else { "idle" }.into();
        }
        self.status = status;
        Ok(())
    }

    pub fn finish(&mut self, method: Method, success: bool) {
        match method {
            Method::Stop if success => {
                self.stop_requested = false;
                self.status = Status {
                    state: "idle".into(),
                    ..Status::default()
                };
            }
            Method::Stop => {
                self.status.error = Some("Hotspot cleanup did not complete.".into());
                self.status.state = "failed".into();
            }
            Method::Start | Method::SwitchBandAndReconnect if !success && !self.stop_requested => {
                self.status.desired_active = false;
                self.status.state = "failed".into();
            }
            _ => {}
        }
    }

    pub fn backend_lost(&mut self) {
        self.failed = true;
        self.status.desired_active = false;
        self.status.state = "failed".into();
        // Preserve last observed active: losing IPC is not evidence of successful cleanup.
        self.status.error = Some("Networking worker disconnected; cleanup is unconfirmed.".into());
    }
}

pub fn validate_reply(method: Method, value: &Value) -> Result<(), &'static str> {
    let valid = match method {
        Method::Stop | Method::SetConfig | Method::PrepareFirewall => value.is_boolean(),
        Method::GetStatus => serde_json::from_value::<Status>(value.clone()).is_ok(),
        Method::GetClients => value.as_array().is_some_and(|items| {
            items.len() <= 1024
                && items.iter().all(|v| {
                    v.as_object().is_some_and(|o| {
                        ["hostname", "ip", "mac"]
                            .iter()
                            .all(|k| o.get(*k).is_none_or(|v| v.is_null() || v.is_string()))
                    })
                })
        }),
        Method::GetConfig => value
            .as_object()
            .is_some_and(|o| o.values().all(Value::is_string)),
        Method::GetInterfaces => ["wifi_interfaces", "all_interfaces"].iter().all(|k| {
            value
                .get(*k)
                .and_then(Value::as_array)
                .is_some_and(|a| a.iter().all(Value::is_string))
        }),
        Method::GetCapabilities => value.is_object(),
        Method::Start | Method::SwitchBandAndReconnect => {
            value.get("success").is_some_and(Value::is_boolean)
                && value
                    .get("error")
                    .is_none_or(|v| v.is_null() || v.is_string())
                && value.get("status").is_none_or(|v| {
                    v.is_null() || serde_json::from_value::<Status>(v.clone()).is_ok()
                })
        }
    };
    if valid {
        Ok(())
    } else {
        Err("Invalid backend reply")
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn stop_wins_over_late_activation() {
        let mut core = Lifecycle::default();
        core.begin(Method::Start).unwrap();
        core.begin(Method::Stop).unwrap();
        core.observe(json!({"active":true,"desired_active":true,"state":"active"}))
            .unwrap();
        core.finish(Method::Start, true);
        assert!(!core.status.desired_active);
        assert_eq!(core.status.state, "stopping");
        assert!(core.status.active);
        core.finish(Method::Stop, true);
        assert!(!core.status.active);
    }
    #[test]
    fn failed_cleanup_does_not_claim_idle() {
        let mut core = Lifecycle::default();
        core.observe(json!({"active":true})).unwrap();
        core.begin(Method::Stop).unwrap();
        core.finish(Method::Stop, false);
        assert!(core.status.active);
        assert_eq!(core.status.state, "failed");
        core.observe(json!({"active":false,"state":"idle","desired_active":true}))
            .unwrap();
        assert_eq!(core.status.state, "failed");
        assert!(!core.status.desired_active);
        assert!(core.status.error.is_some());
        assert!(core.begin(Method::Start).is_err());
        core.begin(Method::Stop).unwrap();
        core.finish(Method::Stop, true);
        assert!(core.begin(Method::Start).is_ok());
    }
    #[test]
    fn lost_worker_cannot_be_revived_by_late_events() {
        let mut core = Lifecycle::default();
        core.backend_lost();
        core.observe(json!({"active":true})).unwrap();
        assert_eq!(core.status.state, "failed");
        assert!(core.begin(Method::Start).is_err());
    }
    #[test]
    fn rejects_ambiguous_or_malformed_replies() {
        for (method, value) in [
            (Method::Stop, json!("true")),
            (Method::Start, json!({"success":1})),
            (Method::GetStatus, json!({"active":true,"client_count":-1})),
            (Method::GetClients, json!([{"mac":1}])),
            (Method::GetConfig, json!({"PASSPHRASE":42})),
            (
                Method::GetInterfaces,
                json!({"all_interfaces":[],"wifi_interfaces":[1]}),
            ),
        ] {
            assert!(validate_reply(method, &value).is_err());
        }
    }
    #[test]
    fn credential_reads_require_authorization() {
        assert!(Method::GetConfig.protected());
        assert!(Method::Stop.protected());
        assert!(!Method::GetStatus.protected());
        assert!(!Method::GetClients.protected());
    }

    #[test]
    fn cancelled_start_reply_cannot_replace_completed_stop() {
        let mut core = Lifecycle::default();
        let start = core.begin(Method::Start).unwrap();
        let stop = core.begin(Method::Stop).unwrap();
        core.complete(Method::Stop, stop, true);
        core.complete(Method::Start, start, false);
        assert_eq!(core.status.state, "idle");
        assert!(!core.status.desired_active);
    }
}
