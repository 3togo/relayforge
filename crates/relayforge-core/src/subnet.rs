//! Shared connected-route checks for the native /24 hotspot subnet.
pub fn check(gateway: &str, routes: &[String]) -> Result<(), &'static str> {
    let gateway = u32::from(
        gateway
            .parse::<std::net::Ipv4Addr>()
            .map_err(|_| "Invalid sharing gateway")?,
    );
    if routes.len() > 4096 {
        return Err("Too many connected routes");
    }
    for route in routes {
        let (network, prefix) = route.split_once('/').unwrap_or((route, "32"));
        let address = u32::from(
            network
                .parse::<std::net::Ipv4Addr>()
                .map_err(|_| "Invalid connected network")?,
        );
        let prefix = prefix
            .parse::<u32>()
            .ok()
            .filter(|v| *v <= 32)
            .ok_or("Invalid connected network prefix")?;
        let bits = prefix.min(24);
        let mask = if bits == 0 {
            0
        } else {
            u32::MAX << (32 - bits)
        };
        if address & mask == gateway & mask {
            return Err("Hotspot subnet overlaps an existing connected network");
        }
    }
    Ok(())
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn overlap_covers_broader_narrower_and_host_routes() {
        for route in [
            "192.168.0.0/16",
            "192.168.12.0/24",
            "192.168.12.128/25",
            "192.168.12.254",
            "0.0.0.0/0",
        ] {
            assert!(check("192.168.12.1", &[route.into()]).is_err());
        }
        assert!(check(
            "192.168.12.1",
            &["192.168.13.0/24".into(), "192.0.2.1/32".into()]
        )
        .is_ok());
        for route in ["invalid", "192.0.2.0/33", "192.0.2.0/-1", "192.0.2.0/24/1"] {
            assert!(check("192.168.12.1", &[route.into()]).is_err());
        }
    }
}
