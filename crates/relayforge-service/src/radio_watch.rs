//! Stop owned sharing when the Wi-Fi radio is switched off.
//!
//! NetworkManager publishes `WirelessEnabled` whenever a hardware or software kill
//! switch changes. Losing the radio invalidates every owned AP, so the service stops
//! sharing instead of reporting a stale active state. Suspend already owns its own
//! cleanup, so a stop is suppressed while the logind watcher is mid-sleep.
use crate::Service;
use futures_util::StreamExt;
use relayforge_core::{Method, OBJECT_PATH};
use serde_json::Value;
use std::collections::HashMap;
use zbus::{zvariant::OwnedValue, Connection, Proxy};

const NM: &str = "org.freedesktop.NetworkManager";
const NM_PATH: &str = "/org/freedesktop/NetworkManager";
const PROPERTIES: &str = "org.freedesktop.DBus.Properties";

async fn disabled(message: &zbus::Message) -> bool {
    let Ok((interface, changed, _)) = message
        .body()
        .deserialize::<(String, HashMap<String, OwnedValue>, Vec<String>)>()
    else {
        return false;
    };
    if interface != NM {
        return false;
    }
    matches!(
        changed.get("WirelessEnabled").map(|value| &**value),
        Some(zbus::zvariant::Value::Bool(enabled)) if !*enabled
    )
}

async fn stop(connection: &Connection) {
    let Ok(interface) = connection
        .object_server()
        .interface::<_, Service>(OBJECT_PATH)
        .await
    else {
        return;
    };
    let service = interface.get().await;
    if service.power.lock().unwrap().sleeping {
        return;
    }
    let _ = service.invoke(Method::Stop, Value::Null, connection).await;
}

/// Resolve the bus-assigned owner so the match rule never depends on name resolution.
async fn owner(connection: &Connection) -> Option<String> {
    let proxy = zbus::fdo::DBusProxy::new(connection).await.ok()?;
    let name = zbus::names::BusName::try_from(NM).ok()?;
    proxy.get_name_owner(name).await.ok().map(|v| v.to_string())
}

pub async fn run(connection: Connection) {
    loop {
        // NetworkManager may start after this service, so keep resolving it.
        let destination = owner(&connection).await;
        let target = destination.as_deref().unwrap_or(NM);
        if let Ok(proxy) = Proxy::new(&connection, target, NM_PATH, PROPERTIES).await {
            if let Ok(mut signals) = proxy.receive_signal("PropertiesChanged").await {
                while let Some(message) = signals.next().await {
                    if disabled(&message).await {
                        stop(&connection).await;
                    }
                }
            }
        }
        tokio::time::sleep(std::time::Duration::from_secs(2)).await;
    }
}
