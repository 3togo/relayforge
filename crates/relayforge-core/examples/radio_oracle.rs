//! Test adapter for differential comparison with the immutable Python reference.
use relayforge_core::radio::{Capture, Snapshot};
use serde_json::json;
use std::io::{self, BufRead};
fn main() {
    for line in io::stdin().lock().lines() {
        let result = serde_json::from_str::<Capture>(&line.unwrap())
            .map_err(|_| "Invalid radio capture")
            .and_then(|record| Snapshot::parse(&record));
        let response = match result {
            Ok(snapshot) => json!({"valid":true,"capabilities":snapshot.capabilities}),
            Err(error) => json!({"valid":false,"error":error}),
        };
        println!("{response}");
    }
}
