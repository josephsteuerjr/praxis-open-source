use std::net::{IpAddr, Ipv4Addr, SocketAddr};
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use url::{Host, Url};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BodyConfig {
    pub device_id: String,
    pub bridge_ws_url: String,
    pub artifact_base_url: String,
    /// Ordered TCP destinations for VPN/hairpin recovery. The canonical URLs still provide
    /// HTTP Host, TLS SNI and certificate identity.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub dial_addresses: Vec<SocketAddr>,
    #[serde(default)]
    pub token: String,
    pub state_dir: PathBuf,
    #[serde(default = "default_chunk_size")]
    pub artifact_chunk_size: u64,
    #[serde(default = "default_system_pipe")]
    pub system_router_pipe: String,
    #[serde(default)]
    pub system_router_token: String,
    #[serde(default = "default_interactive_pipe")]
    pub interactive_router_pipe: String,
    #[serde(default)]
    pub interactive_router_token: String,
    #[serde(default)]
    pub interactive_user_sid: String,
}

fn default_chunk_size() -> u64 {
    4 * 1024 * 1024
}

fn default_system_pipe() -> String {
    r"\\.\pipe\PraxisBodySystem".into()
}

fn default_interactive_pipe() -> String {
    r"\\.\pipe\PraxisBodyInteractive".into()
}

impl BodyConfig {
    pub fn load(path: &Path) -> Result<Self> {
        let raw =
            std::fs::read(path).with_context(|| format!("read body config {}", path.display()))?;
        let mut value: Self = serde_json::from_slice(&raw)
            .with_context(|| format!("parse body config {}", path.display()))?;
        if let Ok(token) = std::env::var("PRAXIS_BODY_TOKEN") {
            value.token = token;
        }
        value.validate()?;
        Ok(value)
    }

    pub fn validate(&self) -> Result<()> {
        let device_id = self.device_id.trim();
        if device_id.is_empty()
            || device_id.len() > 128
            || device_id
                .chars()
                .any(|value| value.is_control() || matches!(value, '/' | '\\' | '?' | '#'))
        {
            anyhow::bail!(
                "device_id must be 1..=128 characters without control or route characters"
            );
        }
        let bridge_url = validate_transport_url(&self.bridge_ws_url, "wss", "ws", "bridge_ws_url")?;
        let artifact_url = validate_transport_url(
            &self.artifact_base_url,
            "https",
            "http",
            "artifact_base_url",
        )?;
        validate_dial_addresses(&self.dial_addresses, &bridge_url, &artifact_url)?;
        if self.state_dir.as_os_str().is_empty() {
            anyhow::bail!("state_dir is required");
        }
        validate_token(&self.token, "body token")?;
        for (value, label) in [
            (&self.system_router_token, "system_router_token"),
            (&self.interactive_router_token, "interactive_router_token"),
        ] {
            if !value.is_empty() {
                validate_token(value, label)?;
            }
        }
        let tokens = [
            self.token.as_str(),
            self.system_router_token.as_str(),
            self.interactive_router_token.as_str(),
        ];
        for left in 0..tokens.len() {
            for right in left + 1..tokens.len() {
                if !tokens[left].is_empty() && tokens[left] == tokens[right] {
                    anyhow::bail!("body and local-router tokens must be distinct");
                }
            }
        }
        Ok(())
    }
}

fn validate_token(value: &str, label: &str) -> Result<()> {
    if value.is_empty() || value.chars().any(char::is_whitespace) {
        anyhow::bail!("{label} is required and must not contain whitespace");
    }
    Ok(())
}

fn validate_transport_url(value: &str, secure: &str, clear: &str, label: &str) -> Result<Url> {
    let parsed = Url::parse(value).with_context(|| format!("parse {label}"))?;
    if !parsed.username().is_empty() || parsed.password().is_some() {
        anyhow::bail!("{label} must not contain embedded credentials");
    }
    if parsed.query().is_some() || parsed.fragment().is_some() {
        anyhow::bail!("{label} must be a base URL without query or fragment");
    }
    match parsed.scheme() {
        scheme if scheme == secure => {}
        scheme if scheme == clear && url_is_loopback(&parsed) => {}
        scheme if scheme == clear => {
            anyhow::bail!("{label} may use {clear}:// only for loopback development")
        }
        _ => anyhow::bail!("{label} must use {secure}:// or loopback {clear}://"),
    }
    Ok(parsed)
}

fn validate_dial_addresses(
    addresses: &[SocketAddr],
    bridge_url: &Url,
    artifact_url: &Url,
) -> Result<()> {
    const MAX_DIAL_ADDRESSES: usize = 8;

    if addresses.is_empty() {
        return Ok(());
    }
    if addresses.len() > MAX_DIAL_ADDRESSES {
        anyhow::bail!("dial_addresses may contain at most {MAX_DIAL_ADDRESSES} entries");
    }

    let (bridge_host, bridge_port) = canonical_dial_endpoint(bridge_url, "bridge_ws_url")?;
    let (artifact_host, artifact_port) =
        canonical_dial_endpoint(artifact_url, "artifact_base_url")?;
    if !bridge_host.eq_ignore_ascii_case(&artifact_host) || bridge_port != artifact_port {
        anyhow::bail!(
            "dial_addresses require bridge_ws_url and artifact_base_url to share one canonical host and port"
        );
    }

    let mut unique = std::collections::HashSet::with_capacity(addresses.len());
    for address in addresses {
        if address.port() == 0 {
            anyhow::bail!("dial_addresses must use an explicit non-zero port");
        }
        if address.port() != bridge_port {
            anyhow::bail!("dial address {address} must use canonical transport port {bridge_port}");
        }
        let ip = address.ip();
        let unsafe_ip = ip.is_unspecified()
            || ip.is_multicast()
            || ip == IpAddr::V4(Ipv4Addr::BROADCAST)
            || match ip {
                IpAddr::V4(value) => value.is_link_local(),
                IpAddr::V6(value) => value.is_unicast_link_local(),
            };
        if unsafe_ip {
            anyhow::bail!(
                "dial address {address} is unspecified, multicast, broadcast or link-local"
            );
        }
        if !unique.insert(*address) {
            anyhow::bail!("dial_addresses contains duplicate {address}");
        }
    }
    Ok(())
}

fn canonical_dial_endpoint(url: &Url, label: &str) -> Result<(String, u16)> {
    let host = match url.host() {
        Some(Host::Domain(host)) => host.to_ascii_lowercase(),
        _ => anyhow::bail!("{label} must use a canonical DNS hostname with dial_addresses"),
    };
    let port = url
        .port_or_known_default()
        .with_context(|| format!("{label} has no known transport port"))?;
    Ok((host, port))
}

fn url_is_loopback(value: &Url) -> bool {
    match value.host() {
        Some(Host::Domain(host)) => host.eq_ignore_ascii_case("localhost"),
        Some(Host::Ipv4(address)) => address.is_loopback(),
        Some(Host::Ipv6(address)) => address.is_loopback(),
        None => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn config() -> BodyConfig {
        BodyConfig {
            device_id: "windows-pc".into(),
            bridge_ws_url: "wss://body.example.invalid".into(),
            artifact_base_url: "https://body.example.invalid".into(),
            dial_addresses: Vec::new(),
            token: "device-token".into(),
            state_dir: PathBuf::from("state"),
            artifact_chunk_size: 4 * 1024 * 1024,
            system_router_pipe: r"\\.\pipe\system".into(),
            system_router_token: "system-token".into(),
            interactive_router_pipe: r"\\.\pipe\interactive".into(),
            interactive_router_token: "interactive-token".into(),
            interactive_user_sid: String::new(),
        }
    }

    #[test]
    fn cleartext_transport_is_loopback_only() {
        let mut value = config();
        value.bridge_ws_url = "ws://127.0.0.1:9473".into();
        value.artifact_base_url = "http://[::1]:9473".into();
        assert!(value.validate().is_ok());

        value.bridge_ws_url = "ws://body.example.invalid".into();
        assert!(
            value
                .validate()
                .unwrap_err()
                .to_string()
                .contains("loopback")
        );
        value.bridge_ws_url = "wss://body.example.invalid".into();
        value.artifact_base_url = "http://body.example.invalid".into();
        assert!(
            value
                .validate()
                .unwrap_err()
                .to_string()
                .contains("loopback")
        );
    }

    #[test]
    fn transport_and_router_tokens_are_separate_compartments() {
        let mut value = config();
        value.system_router_token = value.token.clone();
        assert!(
            value
                .validate()
                .unwrap_err()
                .to_string()
                .contains("distinct")
        );
        value.system_router_token = "system-token".into();
        value.interactive_router_token = "has whitespace".into();
        assert!(
            value
                .validate()
                .unwrap_err()
                .to_string()
                .contains("whitespace")
        );
    }

    #[test]
    fn missing_dial_addresses_is_backward_compatible() {
        let mut encoded = serde_json::to_value(config()).unwrap();
        encoded.as_object_mut().unwrap().remove("dial_addresses");
        let decoded: BodyConfig = serde_json::from_value(encoded).unwrap();
        assert!(decoded.dial_addresses.is_empty());
        assert!(decoded.validate().is_ok());
    }

    #[test]
    fn dial_addresses_are_bounded_explicit_and_share_endpoint_identity() {
        let mut value = config();
        value.dial_addresses = vec![
            "172.29.172.1:443".parse().unwrap(),
            "203.0.113.10:443".parse().unwrap(),
        ];
        assert!(value.validate().is_ok());

        value
            .dial_addresses
            .push("172.29.172.1:443".parse().unwrap());
        assert!(
            value
                .validate()
                .unwrap_err()
                .to_string()
                .contains("duplicate")
        );
        value.dial_addresses.pop();

        for unsafe_address in [
            "0.0.0.0:443",
            "255.255.255.255:443",
            "224.0.0.1:443",
            "169.254.1.1:443",
            "[::]:443",
            "[ff02::1]:443",
            "[fe80::1]:443",
        ] {
            value.dial_addresses[0] = unsafe_address.parse().unwrap();
            assert!(
                value
                    .validate()
                    .unwrap_err()
                    .to_string()
                    .contains("unspecified"),
                "accepted unsafe address {unsafe_address}"
            );
        }
        value.dial_addresses[0] = "172.29.172.1:444".parse().unwrap();
        assert!(
            value
                .validate()
                .unwrap_err()
                .to_string()
                .contains("port 443")
        );

        value.dial_addresses[0] = "172.29.172.1:443".parse().unwrap();
        value.artifact_base_url = "https://artifacts.example.invalid".into();
        assert!(
            value
                .validate()
                .unwrap_err()
                .to_string()
                .contains("share one canonical host")
        );

        let mut encoded = serde_json::to_value(config()).unwrap();
        encoded["dial_addresses"] = serde_json::json!(["not-a-socket-address"]);
        assert!(serde_json::from_value::<BodyConfig>(encoded).is_err());
    }
}
