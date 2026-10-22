//! Owned create_ap firewall rules. Structured argv only; no shell or global flushing.
use crate::{nm_journal::Result, radio::Reply};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
const MAX_OWNED: usize = 64;
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Rule {
    pub table: String,
    pub chain: String,
    pub args: Vec<String>,
}
impl Rule {
    fn new(table: &str, chain: &str, args: &[&str], comment: &str, target: &[&str]) -> Self {
        let mut values: Vec<String> = args.iter().map(|v| v.to_string()).collect();
        values.extend([
            "-m".into(),
            "comment".into(),
            "--comment".into(),
            comment.into(),
        ]);
        values.extend(target.iter().map(|v| v.to_string()));
        Self {
            table: table.into(),
            chain: chain.into(),
            args: values,
        }
    }
    fn argv(&self, operation: &str) -> Vec<String> {
        let mut args = vec![
            "-w".into(),
            "2".into(),
            "-t".into(),
            self.table.clone(),
            operation.into(),
            self.chain.clone(),
        ];
        args.extend(self.args.clone());
        args
    }
}
pub struct Plan {
    tag: String,
    nat: Vec<Rule>,
    dns: Vec<Rule>,
    dhcp: Vec<Rule>,
}
impl Plan {
    pub fn new(fields: &BTreeMap<String, String>) -> Result<Self> {
        let value = |key: &str| {
            fields
                .get(key)
                .map(String::as_str)
                .ok_or("Incomplete firewall intent")
        };
        let iface = value("WIFI_IFACE")?;
        relayforge_core::config::parse_patch(&serde_json::json!({"WIFI_IFACE":iface}).to_string())?;
        let tag = value("RULE_COMMENT")?;
        if !tag.starts_with("wifi-relay-create_ap.")
            || tag.len() > 100
            || !tag
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"_.:-".contains(&b))
        {
            return Err("Invalid firewall ownership marker");
        }
        let gateway = value("GATEWAY")?
            .parse::<std::net::Ipv4Addr>()
            .map_err(|_| "Invalid firewall subnet")?;
        let octets = gateway.octets();
        let subnet = format!("{}.{}.{}.0/24", octets[0], octets[1], octets[2]);
        let gateway = gateway.to_string();
        let method = value("SHARE_METHOD")?;
        if !["nat", "none", "bridge"].contains(&method) {
            return Err("Invalid firewall sharing method");
        }
        let nat = if method == "nat" {
            vec![
                Rule::new(
                    "nat",
                    "POSTROUTING",
                    &["-s", &subnet, "!", "-o", iface],
                    tag,
                    &["-j", "MASQUERADE"],
                ),
                Rule::new(
                    "filter",
                    "FORWARD",
                    &["-i", iface, "-s", &subnet],
                    tag,
                    &["-j", "ACCEPT"],
                ),
                Rule::new(
                    "filter",
                    "FORWARD",
                    &[
                        "-o",
                        iface,
                        "-d",
                        &subnet,
                        "-m",
                        "conntrack",
                        "--ctstate",
                        "ESTABLISHED,RELATED",
                    ],
                    tag,
                    &["-j", "ACCEPT"],
                ),
            ]
        } else {
            Vec::new()
        };
        let mut dns = Vec::new();
        if method != "bridge" && value("NO_DNS")? == "0" && !value("DNS_PORT")?.is_empty() {
            let port = value("DNS_PORT")?
                .parse::<u16>()
                .ok()
                .filter(|p| *p > 0)
                .ok_or("Invalid DNS firewall port")?
                .to_string();
            for protocol in ["tcp", "udp"] {
                dns.push(Rule::new(
                    "filter",
                    "INPUT",
                    &[
                        "-i", iface, "-p", protocol, "-m", protocol, "--dport", &port,
                    ],
                    tag,
                    &["-j", "ACCEPT"],
                ));
            }
            for protocol in ["tcp", "udp"] {
                dns.push(Rule::new(
                    "nat",
                    "PREROUTING",
                    &[
                        "-i", iface, "-s", &subnet, "-d", &gateway, "-p", protocol, "-m", protocol,
                        "--dport", "53",
                    ],
                    tag,
                    &["-j", "REDIRECT", "--to-ports", &port],
                ));
            }
        }
        let dhcp = if method != "bridge" {
            vec![Rule::new(
                "filter",
                "INPUT",
                &["-i", iface, "-p", "udp", "-m", "udp", "--dport", "67"],
                tag,
                &["-j", "ACCEPT"],
            )]
        } else {
            Vec::new()
        };
        Ok(Self {
            tag: tag.into(),
            nat,
            dns,
            dhcp,
        })
    }
    fn all(&self) -> impl Iterator<Item = &Rule> {
        self.nat.iter().chain(&self.dns).chain(&self.dhcp)
    }
    pub async fn apply(&self, phase: &str, executor: &mut impl Executor) -> Result<()> {
        let rules = match phase {
            "nat" => &self.nat,
            "dns" => &self.dns,
            "dhcp" => &self.dhcp,
            _ => return Err("Unknown firewall phase"),
        };
        if rules.is_empty() {
            return Err("Firewall phase is not configured");
        }
        // Refuse unexpected tagged rules before adding anything. Stop can clean
        // exact duplicates but never deletes a rule merely because of its tag.
        let owned = self.count(executor).await?;
        let mut known = 0;
        for rule in self.all() {
            if present(rule, executor).await? {
                known += 1;
            }
        }
        if owned != known {
            return Err("Unexpected or duplicate owned firewall rules; Stop must resolve cleanup");
        }
        for rule in rules {
            if !present(rule, executor).await? {
                let reply = executor.execute(&rule.argv("-I")).await?;
                if reply.code != Some(0) {
                    return Err("Firewall insertion failed; cleanup must verify the saved intent");
                }
            }
            if !present(rule, executor).await? {
                return Err("Firewall insertion was not observed");
            }
        }
        Ok(())
    }
    pub async fn remove(&self, executor: &mut impl Executor) -> Result<()> {
        self.count(executor).await?;
        for rule in self.all() {
            let mut remaining = true;
            for _ in 0..MAX_OWNED {
                if !present(rule, executor).await? {
                    remaining = false;
                    break;
                }
                let reply = executor.execute(&rule.argv("-D")).await?;
                if reply.code != Some(0) && present(rule, executor).await? {
                    return Err("Owned firewall rule removal is unconfirmed");
                }
            }
            if remaining && present(rule, executor).await? {
                return Err("Too many duplicate owned rules");
            }
        }
        self.verify(executor).await
    }
    pub async fn verify(&self, executor: &mut impl Executor) -> Result<()> {
        if self.count(executor).await? != 0 {
            return Err("Owned firewall rules survived cleanup");
        }
        Ok(())
    }
    async fn count(&self, executor: &mut impl Executor) -> Result<usize> {
        let mut count = 0;
        for table in ["filter", "nat"] {
            let reply = executor
                .execute(&[
                    "-w".into(),
                    "2".into(),
                    "-t".into(),
                    table.into(),
                    "-S".into(),
                ])
                .await?;
            if reply.code != Some(0) {
                return Err("Cannot inspect firewall rules");
            }
            for line in reply.stdout.lines().filter(|v| !v.trim().is_empty()) {
                let tokens = tokens(line)?;
                if !matches!(tokens.first().map(String::as_str), Some("-A" | "-P" | "-N")) {
                    return Err("Invalid firewall rule listing");
                }
                if has_tag(&tokens, &self.tag)? {
                    count += 1;
                }
                if count > MAX_OWNED {
                    return Err("Too many owned firewall rules; cleanup is unconfirmed");
                }
            }
        }
        Ok(count)
    }
}
pub(crate) trait Executor {
    async fn execute(&mut self, args: &[String]) -> Result<Reply>;
}
async fn present(rule: &Rule, executor: &mut impl Executor) -> Result<bool> {
    match executor.execute(&rule.argv("-C")).await?.code {
        Some(0) => Ok(true),
        Some(1) => Ok(false),
        _ => Err("Cannot check firewall rule presence"),
    }
}
fn has_tag(tokens: &[String], tag: &str) -> Result<bool> {
    let mut owned = false;
    for (i, token) in tokens.iter().enumerate() {
        if token == "--comment" {
            owned |= tokens.get(i + 1).ok_or("Missing firewall comment value")? == tag;
        } else if let Some(value) = token.strip_prefix("--comment=") {
            owned |= value == tag;
        }
    }
    Ok(owned)
}
// iptables -S is shell-quoted text. Parse only literal words, never evaluate it.
fn tokens(line: &str) -> Result<Vec<String>> {
    let mut result = Vec::new();
    let mut word = String::new();
    let mut quote = None;
    let mut escape = false;
    let mut started = false;
    for c in line.chars() {
        if c == '\0' {
            return Err("Invalid firewall listing data");
        }
        if escape {
            word.push(c);
            escape = false;
            started = true;
            continue;
        }
        match (quote, c) {
            (Some('\''), '\'') | (Some('"'), '"') => quote = None,
            (Some('\''), _) => word.push(c),
            (_, '\\') => {
                escape = true;
                started = true;
            }
            (None, '\'' | '"') => {
                quote = Some(c);
                started = true;
            }
            (None, c) if c.is_whitespace() => {
                if started {
                    result.push(std::mem::take(&mut word));
                    started = false;
                }
            }
            _ => {
                word.push(c);
                started = true;
            }
        }
    }
    if quote.is_some() || escape {
        return Err("Truncated firewall rule listing");
    }
    if started {
        result.push(word);
    }
    Ok(result)
}
#[derive(Default, Deserialize, Serialize)]
#[serde(default, deny_unknown_fields)]
pub struct Simulator {
    pub rules: Vec<Rule>,
    pub calls: Vec<Vec<String>>,
    pub mutations: usize,
    pub fail_mutation: Option<usize>,
    pub lose_reply: Option<usize>,
    pub fail_delete: bool,
    pub fail_check: bool,
    pub fail_list: bool,
    pub listing: Option<String>,
}
impl Simulator {
    pub fn execute(&mut self, args: &[String]) -> Result<Reply> {
        if self.calls.len() >= 2048 {
            return Err("Simulated firewall call limit exceeded");
        }
        self.calls.push(args.to_vec());
        if args.len() < 5
            || args[0] != "-w"
            || args[1] != "2"
            || args[2] != "-t"
            || !["nat", "filter"].contains(&args[3].as_str())
        {
            return Err("Invalid simulated firewall command");
        }
        let table = &args[3];
        let operation = args[4].as_str();
        let mut code = 0;
        let mut stdout = String::new();
        match operation {
            "-S" => {
                if self.fail_list {
                    code = 4;
                } else if let Some(text) = &self.listing {
                    stdout = text.clone();
                } else {
                    for rule in self.rules.iter().filter(|r| r.table == *table) {
                        stdout.push_str(&format!(
                            "-A {} {}\n",
                            rule.chain,
                            rule.args
                                .iter()
                                .map(|s| format!("'{}'", s.replace('\'', "'\"'\"'")))
                                .collect::<Vec<_>>()
                                .join(" ")
                        ));
                    }
                }
            }
            "-C" | "-I" | "-D" if args.len() >= 6 => {
                let rule = Rule {
                    table: table.clone(),
                    chain: args[5].clone(),
                    args: args[6..].to_vec(),
                };
                let position = self.rules.iter().position(|r| *r == rule);
                if operation == "-C" {
                    code = if self.fail_check {
                        4
                    } else if position.is_some() {
                        0
                    } else {
                        1
                    };
                } else {
                    self.mutations += 1;
                    if self.fail_mutation == Some(self.mutations)
                        || operation == "-D" && self.fail_delete
                    {
                        code = 4;
                    } else if operation == "-I" {
                        self.rules.push(rule);
                    } else if let Some(i) = position {
                        self.rules.remove(i);
                    } else {
                        code = 1;
                    }
                    if self.lose_reply == Some(self.mutations) {
                        code = 4;
                    }
                }
            }
            _ => return Err("Unsupported simulated firewall operation"),
        }
        Ok(Reply {
            code: Some(code),
            stdout,
        })
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn comment_ownership_is_exact_and_shell_text_is_not_evaluated() {
        let tag = "wifi-relay-create_ap.demo0.conf.ABCDEFGH";
        assert!(has_tag(
            &tokens(&format!("-A INPUT --comment \"{tag}\" -j ACCEPT")).unwrap(),
            tag
        )
        .unwrap());
        assert!(!has_tag(
            &tokens(&format!("-A INPUT --comment '{tag}-foreign' -j ACCEPT")).unwrap(),
            tag
        )
        .unwrap());
        assert_eq!(
            tokens("-A INPUT --comment '$(touch /tmp/x)' --jump ACCEPT").unwrap()[3],
            "$(touch /tmp/x)"
        );
        assert!(tokens("-A INPUT --comment 'truncated").is_err());
        assert!(tokens("-A INPUT \\").is_err());
    }
}
