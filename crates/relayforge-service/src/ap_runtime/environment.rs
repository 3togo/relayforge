//! Observe administrator policy; never set country, load modules or edit firewalld.
use super::*;
use relayforge_core::config::Config;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Fixture {
    firewalld_active: bool,
    regulatory: String,
}
fn fixture(root: &Path) -> Result<Fixture> {
    let bytes = Directory::open(root)?
        .read("policy-state.json", true, LIMIT)?
        .ok_or("Missing simulated administrator policy")?;
    serde_json::from_slice(&bytes).map_err(|_| "Invalid simulated administrator policy")
}
pub(super) async fn firewall(p: &Platform, method: &str) -> Result<()> {
    if method == "bridge" {
        return Ok(());
    }
    let active = if let Some(root) = &p.fixture {
        fixture(root)?.firewalld_active
    } else {
        tokio::time::timeout(Duration::from_secs(2), async {
            // Ask only the bus daemon: do not activate or call firewalld itself.
            let bus = zbus::connection::Builder::address("unix:path=/run/dbus/system_bus_socket")
                .map_err(|_| "Cannot inspect firewall policy bus")?
                .build()
                .await
                .map_err(|_| "Cannot inspect firewall policy bus")?;
            let proxy = zbus::fdo::DBusProxy::new(&bus)
                .await
                .map_err(|_| "Cannot inspect firewall policy owner")?;
            proxy
                .name_has_owner("org.fedoraproject.FirewallD1".try_into().unwrap())
                .await
                .map_err(|_| "Cannot inspect firewall policy owner")
        })
        .await
        .map_err(|_| "Firewall policy inspection timed out")??
    };
    if active {
        Err("Active firewalld is not supported by native scoped iptables; select the NetworkManager backend with its supported defaults or use a validated administrator firewall integration; leave firewalld running")
    } else {
        Ok(())
    }
}

fn regulatory_proof(phy: u32, requested: &str, text: &str) -> Result<String> {
    if text.len() > LIMIT
        || !text.is_ascii()
        || text
            .bytes()
            .any(|b| b.is_ascii_control() && !b"\n\r\t".contains(&b))
    {
        return Err("Invalid regulatory observation");
    }
    let lines: Vec<_> = text
        .lines()
        .map(str::trim)
        .filter(|v| !v.is_empty())
        .collect();
    let name = format!("phy#{phy}");
    if lines
        .first()
        .is_none_or(|v| *v != name && *v != format!("{name} (self-managed)"))
    {
        return Err("Ambiguous or changed regulatory radio identity");
    }
    let country = lines
        .get(1)
        .and_then(|v| v.strip_prefix("country "))
        .and_then(|v| v.split_once(": "))
        .ok_or("Missing regulatory country observation")?;
    if country.0.len() != 2
        || !country
            .0
            .bytes()
            .all(|b| b.is_ascii_uppercase() || b.is_ascii_digit())
        || !["DFS-UNSET", "DFS-FCC", "DFS-ETSI", "DFS-JP"].contains(&country.1)
        || lines.len() < 3
        || lines[2..]
            .iter()
            .any(|v| !v.starts_with('(') || !v.contains(')'))
    {
        return Err("Incomplete or ambiguous regulatory observation");
    }
    if country.0 != requested {
        return Err("Configured COUNTRY must match the radio's existing regulatory domain; provision it through the administrator's normal network policy or remove COUNTRY; RelayForge does not change country");
    }
    Ok(format!("{:x}", Sha256::digest(lines.join("\n").as_bytes())))
}
pub(super) async fn regulatory(p: &Platform, config: &Config, phy: u32) -> Result<Option<String>> {
    let Some(country) = config.get("COUNTRY") else {
        return Ok(None);
    };
    let text = if let Some(root) = &p.fixture {
        fixture(root)?.regulatory
    } else {
        let iw = ["/usr/sbin/iw", "/usr/bin/iw", "/sbin/iw", "/bin/iw"]
            .into_iter()
            .map(Path::new)
            .find(|p| p.is_file())
            .ok_or("Regulatory inspection utility unavailable")?;
        radio::command(
            iw,
            &["phy", &format!("phy{phy}"), "reg", "get"],
            Duration::from_secs(2),
        )
        .await?
    };
    regulatory_proof(phy, country, &text).map(Some)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn text() -> &'static str {
        "phy#0\ncountry US: DFS-FCC\n\t(2402 - 2472 @ 40), (N/A, 20), (N/A)\n"
    }
    #[test]
    fn accepts_only_the_requested_radio_and_existing_country() {
        let proof = regulatory_proof(0, "US", text()).unwrap();
        assert_eq!(proof.len(), 64);
        assert_eq!(
            regulatory_proof(0, "US", &text().replace("phy#0", "phy#0 (self-managed)"))
                .unwrap()
                .len(),
            64
        );
        assert!(regulatory_proof(1, "US", text()).is_err());
        assert!(regulatory_proof(0, "GB", text()).is_err());
        assert_ne!(
            proof,
            regulatory_proof(0, "US", &text().replace("20)", "10)")).unwrap()
        );
    }
    #[test]
    fn incomplete_global_duplicate_and_unrecognized_observations_are_refused() {
        for value in [
            "",
            "global\ncountry US: DFS-FCC\n(1 - 2 @ 1)\n",
            "phy#0\ncountry US: DFS-FCC\n",
            "phy#0\ncountry US: DFS-new\n(1 - 2 @ 1)\n",
            "phy#0\ncountry US: DFS-FCC\ncountry GB: DFS-ETSI\n(1 - 2 @ 1)\n",
        ] {
            assert!(regulatory_proof(0, "US", value).is_err());
        }
        assert!(regulatory_proof(0, "US", &"x".repeat(LIMIT + 1)).is_err());
        assert!(regulatory_proof(0, "US", &format!("{}\u{1b}", text())).is_err());
    }
}
