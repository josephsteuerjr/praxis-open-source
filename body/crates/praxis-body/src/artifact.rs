use std::collections::HashSet;
use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::time::Duration;

use anyhow::{Context, Result, bail};
use praxis_body_protocol::{ArtifactRef, ArtifactStatus};
use reqwest::StatusCode;
use reqwest::dns::{Addrs, Name, Resolve, Resolving};
use serde::Deserialize;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

use crate::config::BodyConfig;
use crate::fsops;

#[derive(Clone)]
pub struct ArtifactClient {
    client: reqwest::Client,
    base_url: String,
    token: String,
    device_id: String,
}

#[derive(Clone)]
struct DialResolver {
    canonical_host: String,
    preferred: Vec<SocketAddr>,
}

impl DialResolver {
    fn new(canonical_host: &str, preferred: &[SocketAddr]) -> Self {
        Self {
            canonical_host: canonical_host.to_ascii_lowercase(),
            preferred: preferred.to_vec(),
        }
    }
}

fn merge_resolved_addresses(
    preferred: Vec<SocketAddr>,
    resolved: impl IntoIterator<Item = SocketAddr>,
) -> Vec<SocketAddr> {
    const MAX_DNS_ADDRESSES: usize = 8;
    let mut unique = HashSet::new();
    preferred
        .into_iter()
        .chain(resolved.into_iter().take(MAX_DNS_ADDRESSES))
        // reqwest rewrites DNS port zero to the URL port, so deduplicate by IP before that
        // normalization instead of attempting the same endpoint twice as `:443` and `:0`.
        .filter(|address| unique.insert(address.ip()))
        .collect()
}

impl Resolve for DialResolver {
    fn resolve(&self, name: Name) -> Resolving {
        let host = name.as_str().to_string();
        let preferred = if host.eq_ignore_ascii_case(&self.canonical_host) {
            self.preferred.clone()
        } else {
            Vec::new()
        };
        Box::pin(async move {
            // Port zero lets reqwest apply the URL's canonical/default port. Configured addresses
            // already use that same validated port and remain first in connector order.
            let dns = tokio::time::timeout(
                Duration::from_secs(5),
                tokio::net::lookup_host((host.as_str(), 0)),
            )
            .await;
            let addresses = match dns {
                Ok(Ok(resolved)) => merge_resolved_addresses(preferred, resolved),
                Ok(Err(error)) if preferred.is_empty() => {
                    return Err(Box::new(error) as Box<dyn std::error::Error + Send + Sync>);
                }
                Err(error) if preferred.is_empty() => {
                    return Err(Box::new(error) as Box<dyn std::error::Error + Send + Sync>);
                }
                _ => preferred,
            };
            Ok(Box::new(addresses.into_iter()) as Addrs)
        })
    }
}

const MIN_CHUNK_SIZE: u64 = 64 * 1024;
const MAX_CHUNK_SIZE: u64 = 16 * 1024 * 1024;
const MAX_ARTIFACT_SIZE: u64 = 16 * 1024 * 1024 * 1024 * 1024;
const MAX_ARTIFACT_NAME_CHARS: usize = 255;
const MAX_ARTIFACT_NAME_BYTES: usize = 1_024;

#[derive(Debug, Deserialize)]
struct ExportArgs {
    path: PathBuf,
    #[serde(default)]
    name: Option<String>,
}

#[derive(Debug, Deserialize)]
struct ImportArgs {
    artifact: ArtifactRef,
    path: PathBuf,
    #[serde(default)]
    expected_sha256: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum LocalResumePolicy {
    Resume(u64),
    Restart(&'static str),
}

fn canonical_sha256(value: &str) -> Result<String> {
    if value.len() != 64 || !value.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        bail!("sha256 must be exactly 64 hexadecimal characters")
    }
    Ok(value.to_ascii_lowercase())
}

fn validate_artifact_name(name: &str) -> Result<()> {
    if name.trim().is_empty() || matches!(name, "." | "..") {
        bail!("artifact name must not be empty or a dot path")
    }
    if name.chars().count() > MAX_ARTIFACT_NAME_CHARS || name.len() > MAX_ARTIFACT_NAME_BYTES {
        bail!(
            "artifact name exceeds {MAX_ARTIFACT_NAME_CHARS} characters or {MAX_ARTIFACT_NAME_BYTES} UTF-8 bytes"
        )
    }
    if name.ends_with(' ')
        || name.ends_with('.')
        || name.chars().any(|value| {
            value.is_control()
                || matches!(value, '<' | '>' | ':' | '"' | '/' | '\\' | '|' | '?' | '*')
        })
    {
        bail!("artifact name must be one portable file name without control or path characters")
    }
    let stem = name
        .split('.')
        .next()
        .unwrap_or_default()
        .to_ascii_uppercase();
    let reserved = matches!(stem.as_str(), "CON" | "PRN" | "AUX" | "NUL")
        || stem
            .strip_prefix("COM")
            .and_then(|value| value.parse::<u8>().ok())
            .is_some_and(|value| (1..=9).contains(&value))
        || stem
            .strip_prefix("LPT")
            .and_then(|value| value.parse::<u8>().ok())
            .is_some_and(|value| (1..=9).contains(&value));
    if reserved {
        bail!("artifact name is reserved by Windows")
    }
    Ok(())
}

fn validate_artifact_size(size: u64) -> Result<()> {
    if size > MAX_ARTIFACT_SIZE {
        bail!("artifact size {size} exceeds the {MAX_ARTIFACT_SIZE}-byte client limit")
    }
    Ok(())
}

fn validated_artifact(artifact: &ArtifactRef) -> Result<ArtifactRef> {
    let mut value = artifact.clone();
    value.sha256 = canonical_sha256(&value.sha256)?;
    validate_artifact_size(value.size)?;
    validate_artifact_name(&value.name)?;
    Ok(value)
}

fn validate_chunk_size(chunk_size: u64) -> Result<u64> {
    if !(MIN_CHUNK_SIZE..=MAX_CHUNK_SIZE).contains(&chunk_size) {
        bail!(
            "server artifact chunk_size {chunk_size} is outside {MIN_CHUNK_SIZE}..={MAX_CHUNK_SIZE}"
        )
    }
    Ok(chunk_size)
}

fn validate_received_offsets(offsets: &[u64], size: u64, chunk_size: u64) -> Result<HashSet<u64>> {
    let chunk_size = validate_chunk_size(chunk_size)?;
    let expected_chunks = if size == 0 {
        0
    } else {
        size.div_ceil(chunk_size)
    };
    if offsets.len() as u64 > expected_chunks {
        bail!("server returned more artifact offsets than the declared size can contain")
    }
    let mut received = HashSet::with_capacity(offsets.len());
    for &offset in offsets {
        if offset >= size {
            bail!("server resume offset {offset} is outside artifact size {size}")
        }
        if !offset.is_multiple_of(chunk_size) {
            bail!(
                "server resume offset {offset} is incompatible with authoritative chunk_size {chunk_size}; reset the incomplete server artifact"
            )
        }
        if !received.insert(offset) {
            bail!("server returned duplicate artifact resume offset {offset}")
        }
    }
    Ok(received)
}

fn local_resume_policy(length: u64, size: u64, chunk_size: u64) -> LocalResumePolicy {
    if chunk_size == 0 {
        LocalResumePolicy::Restart("invalid_server_chunk_size")
    } else if length > size {
        LocalResumePolicy::Restart("stage_larger_than_artifact")
    } else if length != size && !length.is_multiple_of(chunk_size) {
        LocalResumePolicy::Restart("stage_offset_incompatible_with_server_chunk_size")
    } else {
        LocalResumePolicy::Resume(length)
    }
}

impl ArtifactClient {
    pub fn new(config: &BodyConfig) -> Result<Self> {
        let base_url = config.artifact_base_url.trim_end_matches('/').to_string();
        let parsed_base = url::Url::parse(&base_url).context("parse artifact_base_url")?;
        let mut builder = reqwest::Client::builder()
            .connect_timeout(Duration::from_secs(10))
            .timeout(Duration::from_secs(120));
        if !config.dial_addresses.is_empty() {
            let host = parsed_base
                .host_str()
                .context("artifact_base_url has no canonical host")?;
            builder = builder.dns_resolver(DialResolver::new(host, &config.dial_addresses));
        }
        Ok(Self {
            client: builder.build()?,
            base_url,
            token: config.token.clone(),
            device_id: config.device_id.clone(),
        })
    }

    fn auth(&self, request: reqwest::RequestBuilder) -> reqwest::RequestBuilder {
        request.bearer_auth(&self.token)
    }

    async fn status(&self, sha256: &str) -> Result<Option<ArtifactStatus>> {
        let sha256 = canonical_sha256(sha256)?;
        let response = self
            .auth(
                self.client
                    .get(format!("{}/v1/artifacts/{sha256}", self.base_url)),
            )
            .send()
            .await?;
        if response.status() == StatusCode::NOT_FOUND {
            return Ok(None);
        }
        let response = response.error_for_status()?;
        let mut status: ArtifactStatus = response.json().await?;
        status.artifact = validated_artifact(&status.artifact)
            .context("invalid artifact metadata returned by server")?;
        if status.artifact.sha256 != sha256 {
            bail!(
                "server artifact hash {} does not match requested {sha256}",
                status.artifact.sha256
            )
        }
        status.chunk_size = validate_chunk_size(status.chunk_size)?;
        validate_received_offsets(
            &status.received_offsets,
            status.artifact.size,
            status.chunk_size,
        )?;
        Ok(Some(status))
    }

    pub async fn dispatch(&self, capability: &str, args: Value) -> Result<Value> {
        match capability {
            "fs.export" => {
                let args: ExportArgs = serde_json::from_value(args)?;
                let artifact = self.upload(&args.path, args.name.as_deref()).await?;
                Ok(json!({"ok": true, "artifact": artifact}))
            }
            "fs.import" => {
                let args: ImportArgs = serde_json::from_value(args)?;
                self.download(&args.artifact, &args.path, args.expected_sha256.as_deref())
                    .await
            }
            _ => anyhow::bail!("unknown artifact capability {capability}"),
        }
    }

    pub async fn upload(&self, path: &Path, name: Option<&str>) -> Result<ArtifactRef> {
        let (sha256, size) = fsops::sha256_file(path)?;
        let sha256 = canonical_sha256(&sha256)?;
        validate_artifact_size(size)?;
        let name = name
            .map(str::to_string)
            .or_else(|| path.file_name().map(|x| x.to_string_lossy().to_string()))
            .unwrap_or_else(|| sha256.clone());
        validate_artifact_name(&name)?;
        let mime = mime_guess::from_path(path).first_raw().map(str::to_string);
        let artifact = validated_artifact(&ArtifactRef {
            sha256: sha256.clone(),
            size,
            name: name.clone(),
            mime: mime.clone(),
            source_device: Some(self.device_id.clone()),
        })?;
        self.auth(
            self.client
                .post(format!("{}/v1/artifacts/{}/offer", self.base_url, sha256)),
        )
        .json(&artifact)
        .send()
        .await?
        .error_for_status()?;
        let status = self
            .status(&sha256)
            .await?
            .context("artifact offer succeeded but server returned no status")?;
        if status.artifact.size != size {
            bail!(
                "server artifact size {} does not match local size {size}",
                status.artifact.size
            )
        }
        if status.complete {
            return Ok(artifact);
        }
        let chunk_size = status.chunk_size;
        let received = validate_received_offsets(&status.received_offsets, size, chunk_size)?;
        let mut file = tokio::fs::File::open(path)
            .await
            .with_context(|| format!("open artifact {}", path.display()))?;
        let mut offset = 0u64;
        while offset < size {
            let take = usize::try_from(chunk_size.min(size - offset))?;
            let mut chunk = vec![0u8; take];
            file.read_exact(&mut chunk).await?;
            if !received.contains(&offset) {
                let chunk_sha = hex::encode(Sha256::digest(&chunk));
                self.auth(self.client.put(format!(
                    "{}/v1/artifacts/{}/chunks/{offset}",
                    self.base_url, sha256
                )))
                .header("x-praxis-total-size", size)
                .header("x-praxis-name-hex", hex::encode(name.as_bytes()))
                .header("x-praxis-chunk-sha256", chunk_sha)
                .header("x-praxis-device-id", &self.device_id)
                .header(
                    "x-praxis-mime",
                    mime.as_deref().unwrap_or("application/octet-stream"),
                )
                .body(chunk)
                .send()
                .await?
                .error_for_status()?;
            }
            offset += take as u64;
        }
        self.auth(self.client.post(format!(
            "{}/v1/artifacts/{}/complete",
            self.base_url, sha256
        )))
        .send()
        .await?
        .error_for_status()?;
        let completed = self
            .status(&sha256)
            .await?
            .context("artifact completion succeeded but server returned no status")?;
        if !completed.complete
            || completed.artifact.sha256 != sha256
            || completed.artifact.size != size
        {
            bail!("server did not confirm the completed artifact hash and size")
        }
        Ok(artifact)
    }

    pub async fn download(
        &self,
        artifact: &ArtifactRef,
        destination: &Path,
        expected_sha256: Option<&str>,
    ) -> Result<Value> {
        let artifact = validated_artifact(artifact)?;
        if let Some(expected) = expected_sha256 {
            let expected = canonical_sha256(expected).context("expected_sha256 is malformed")?;
            let actual = fsops::existing_hash(destination)?;
            if actual.as_deref() != Some(expected.as_str()) {
                anyhow::bail!(
                    "hash conflict: expected {}, actual {}",
                    expected,
                    actual.as_deref().unwrap_or("<missing>")
                );
            }
        }
        let status = self
            .status(&artifact.sha256)
            .await?
            .context("artifact is unknown to the server")?;
        if !status.complete {
            bail!("artifact is not complete on the server")
        }
        if status.artifact.size != artifact.size {
            bail!(
                "server artifact size {} does not match requested size {}",
                status.artifact.size,
                artifact.size
            )
        }
        let chunk_size = status.chunk_size;
        let parent = destination.parent().context("destination has no parent")?;
        tokio::fs::create_dir_all(parent).await?;
        let file_name = destination
            .file_name()
            .context("destination has no file name")?
            .to_string_lossy();
        let stage_tag = artifact
            .sha256
            .get(..12)
            .context("validated artifact hash has no stage prefix")?;
        let stage = parent.join(format!(".{file_name}.praxis-part-{}", stage_tag));
        let stage_length = match tokio::fs::metadata(&stage).await {
            Ok(meta) => Some(meta.len()),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
            Err(error) => return Err(error.into()),
        };
        let (mut offset, mut resume_reset) = match stage_length
            .map(|length| local_resume_policy(length, artifact.size, chunk_size))
        {
            Some(LocalResumePolicy::Resume(offset)) => (offset, None),
            Some(LocalResumePolicy::Restart(reason)) => {
                tokio::fs::remove_file(&stage).await?;
                (0, Some(reason))
            }
            None => (0, None),
        };
        if stage_length == Some(artifact.size) {
            let (stage_sha, stage_size) = fsops::sha256_file(&stage)?;
            if canonical_sha256(&stage_sha)? != artifact.sha256 || stage_size != artifact.size {
                tokio::fs::remove_file(&stage).await?;
                offset = 0;
                resume_reset = Some("stage_complete_hash_mismatch");
            }
        }
        let resumed_from = offset;
        let mut output = tokio::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&stage)
            .await?;
        while offset < artifact.size {
            let length = chunk_size.min(artifact.size - offset);
            let mut url = url::Url::parse(&format!(
                "{}/v1/artifacts/{}/content",
                self.base_url, artifact.sha256
            ))?;
            url.query_pairs_mut()
                .append_pair("offset", &offset.to_string())
                .append_pair("length", &length.to_string());
            let response = self
                .auth(self.client.get(url))
                .send()
                .await?
                .error_for_status()?;
            let response_total = response
                .headers()
                .get("x-praxis-total-size")
                .and_then(|value| value.to_str().ok())
                .and_then(|value| value.parse::<u64>().ok())
                .context("artifact response has no valid x-praxis-total-size")?;
            let response_offset = response
                .headers()
                .get("x-praxis-offset")
                .and_then(|value| value.to_str().ok())
                .and_then(|value| value.parse::<u64>().ok())
                .context("artifact response has no valid x-praxis-offset")?;
            if response_total != artifact.size || response_offset != offset {
                bail!(
                    "artifact response range mismatch: expected {offset}/{}, got {response_offset}/{response_total}",
                    artifact.size
                )
            }
            let chunk = response.bytes().await?;
            if chunk.len() as u64 != length {
                bail!(
                    "artifact download returned {} bytes at {offset}, expected {length}",
                    chunk.len()
                )
            }
            output.write_all(&chunk).await?;
            output.flush().await?;
            offset += chunk.len() as u64;
        }
        output.sync_all().await?;
        drop(output);
        let (actual_sha, actual_size) = fsops::sha256_file(&stage)?;
        if canonical_sha256(&actual_sha)? != artifact.sha256 || actual_size != artifact.size {
            let _ = tokio::fs::remove_file(&stage).await;
            anyhow::bail!("downloaded artifact hash or size mismatch");
        }
        fsops::replace_path(&stage, destination)?;
        Ok(json!({
            "ok": true,
            "path": destination,
            "artifact": artifact,
            "resumed_from": resumed_from,
            "resume_reset": resume_reset,
            "chunk_size": chunk_size,
        }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn artifact(size: u64) -> ArtifactRef {
        ArtifactRef {
            sha256: "A".repeat(64),
            size,
            name: "Отчёт 2026.txt".into(),
            mime: Some("text/plain".into()),
            source_device: Some("windows-pc".into()),
        }
    }

    #[test]
    fn artifact_metadata_is_strict_and_hash_is_canonicalized_before_slicing() {
        let value = validated_artifact(&artifact(7)).unwrap();
        assert_eq!(value.sha256, "a".repeat(64));

        for malformed in ["", "abc", "../secret", &"g".repeat(64)] {
            assert!(canonical_sha256(malformed).is_err());
        }
        for name in [
            "",
            ".",
            "..",
            "folder/file.txt",
            r"folder\file.txt",
            "NUL.txt",
            "bad.",
        ] {
            let mut value = artifact(7);
            value.name = name.into();
            assert!(validated_artifact(&value).is_err(), "accepted {name:?}");
        }
        assert!(validate_artifact_size(MAX_ARTIFACT_SIZE).is_ok());
        assert!(validate_artifact_size(MAX_ARTIFACT_SIZE + 1).is_err());
    }

    #[test]
    fn server_chunk_size_and_offsets_form_one_authoritative_resume_grid() {
        let chunk = MIN_CHUNK_SIZE;
        let size = chunk * 3 - 17;
        let valid = validate_received_offsets(&[0, chunk, chunk * 2], size, chunk).unwrap();
        assert_eq!(valid.len(), 3);

        assert!(validate_chunk_size(0).is_err());
        assert!(validate_chunk_size(MAX_CHUNK_SIZE + 1).is_err());
        assert!(validate_received_offsets(&[], size, 0).is_err());
        assert!(validate_received_offsets(&[1], size, chunk).is_err());
        assert!(validate_received_offsets(&[0, 0], size, chunk).is_err());
        assert!(validate_received_offsets(&[size], size, chunk).is_err());
        assert!(validate_received_offsets(&[0], 0, chunk).is_err());
    }

    #[test]
    fn local_resume_policy_restarts_only_incompatible_stage_offsets() {
        let chunk = MIN_CHUNK_SIZE;
        let size = chunk * 3 + 9;
        assert_eq!(
            local_resume_policy(chunk, size, chunk),
            LocalResumePolicy::Resume(chunk)
        );
        assert_eq!(
            local_resume_policy(size, size, chunk),
            LocalResumePolicy::Resume(size)
        );
        assert_eq!(
            local_resume_policy(chunk + 1, size, chunk),
            LocalResumePolicy::Restart("stage_offset_incompatible_with_server_chunk_size")
        );
        assert_eq!(
            local_resume_policy(size + 1, size, chunk),
            LocalResumePolicy::Restart("stage_larger_than_artifact")
        );
        assert_eq!(
            local_resume_policy(0, size, 0),
            LocalResumePolicy::Restart("invalid_server_chunk_size")
        );
    }

    #[test]
    fn preferred_artifact_routes_stay_first_and_are_deduplicated() {
        let first: SocketAddr = "172.29.172.1:443".parse().unwrap();
        let second: SocketAddr = "203.0.113.10:443".parse().unwrap();
        let dns_duplicate = SocketAddr::new(second.ip(), 0);
        let dns_only: SocketAddr = "198.51.100.7:0".parse().unwrap();
        assert_eq!(
            merge_resolved_addresses(vec![first, second], [dns_duplicate, dns_only]),
            vec![first, second, dns_only]
        );
    }

    #[tokio::test]
    async fn artifact_resolver_appends_direct_dns_after_matching_candidates() {
        let preferred: SocketAddr = "127.0.0.2:443".parse().unwrap();
        let resolver = DialResolver::new("localhost", &[preferred]);
        let resolved: Vec<_> = resolver
            .resolve("localhost".parse().unwrap())
            .await
            .unwrap()
            .collect();
        assert_eq!(resolved.first(), Some(&preferred));
        assert!(
            resolved
                .iter()
                .skip(1)
                .any(|address| address.ip().is_loopback() && address.port() == 0)
        );
    }
}
