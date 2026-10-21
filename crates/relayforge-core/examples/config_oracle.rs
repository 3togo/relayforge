//! Differential-test adapter. This example is not installed as a service command.
use relayforge_core::config::{parse_file, parse_patch, printable, validate};
use serde_json::{json, Value};
use std::io::{self, BufRead, Write};
fn main() {
    if std::env::args().nth(1).as_deref() == Some("--printable-bitmap") {
        let bitmap: Vec<u8> = (0..=0x10ffff)
            .map(|n| u8::from(char::from_u32(n).is_some_and(printable)))
            .collect();
        io::stdout().write_all(&bitmap).unwrap();
        return;
    }
    for line in io::stdin().lock().lines() {
        let request: Value = serde_json::from_str(&line.unwrap()).unwrap();
        let result = if let Some(file) = request.get("file").and_then(Value::as_str) {
            parse_file(file)
        } else {
            parse_patch(&request["config"].to_string()).and_then(|c| validate(&c))
        };
        let response = match result {
            Ok(config) => json!({"valid":true,"config":config}),
            Err(_) => json!({"valid":false}),
        };
        println!("{response}");
    }
}
