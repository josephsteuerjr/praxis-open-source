use std::collections::BTreeMap;
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::PathBuf;

use axum::Json;
use axum::body::Bytes;
use axum::extract::{Path as AxumPath, Query, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use praxis_body_protocol::{ArtifactRef, ArtifactStatus};
use serde::{Deserialize, Serialize};
use serde_json::json;
use sha2::{Digest, Sha256};

use crate::AppState;

#[derive(Clone)]
pub struct ArtifactStore {
    root: PathBuf,
    chunk_size: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct IncomingMeta {
    artifact: ArtifactRef,
    chunk_size: u64,
}

#[derive(Debug, Deserialize)]
pub struct DownloadQuery {
    #[serde(default)]
    offset: u64,
    #[serde(default)]
    length: Option<u64>,
}

impl ArtifactStore {
    pub fn open(root: PathBuf, chunk_size: u64) -> anyhow::Result<Self> {
        std::fs::create_dir_all(root.join("incoming"))?;
        std::fs::create_dir_all(root.join("cas"))?;
        Ok(Self {
            root,
            chunk_size: chunk_size.clamp(64 * 1024, 16 * 1024 * 1024),
        })
    }

    fn validate_sha(sha: &str) -> Result<(), String> {
        if sha.len() == 64 && sha.bytes().all(|b| b.is_ascii_hexdigit()) {
            Ok(())
        } else {
            Err("sha256 must be 64 hexadecimal characters".into())
        }
    }

    fn incoming(&self, sha: &str) -> PathBuf {
        self.root.join("incoming").join(sha.to_ascii_lowercase())
    }

    fn cas(&self, sha: &str) -> PathBuf {
        self.root
            .join("cas")
            .join(sha[..2].to_ascii_lowercase())
            .join(sha.to_ascii_lowercase())
    }

    fn read_meta(&self, sha: &str) -> Result<IncomingMeta, String> {
        let raw = std::fs::read(self.incoming(sha).join("meta.json"))
            .map_err(|e| format!("artifact metadata: {e}"))?;
        serde_json::from_slice(&raw).map_err(|e| format!("artifact metadata: {e}"))
    }

    fn read_cas_meta(&self, sha: &str) -> Option<IncomingMeta> {
        std::fs::read(self.cas(sha).with_extension("json"))
            .ok()
            .and_then(|raw| serde_json::from_slice(&raw).ok())
    }

    fn offsets(&self, sha: &str) -> Result<Vec<u64>, String> {
        let chunks = self.incoming(sha).join("chunks");
        let mut offsets = Vec::new();
        if !chunks.is_dir() {
            return Ok(offsets);
        }
        for entry in std::fs::read_dir(chunks).map_err(|e| e.to_string())? {
            let entry = entry.map_err(|e| e.to_string())?;
            if let Some(value) = entry
                .path()
                .file_stem()
                .and_then(|x| x.to_str())
                .and_then(|x| x.parse::<u64>().ok())
            {
                offsets.push(value);
            }
        }
        offsets.sort_unstable();
        Ok(offsets)
    }
}

fn json_error(status: StatusCode, message: impl Into<String>) -> Response {
    (status, Json(json!({"ok": false, "error": message.into()}))).into_response()
}

fn authorized(state: &AppState, headers: &HeaderMap) -> bool {
    state.authorized_any(headers)
}

pub async fn status(
    State(state): State<AppState>,
    AxumPath(sha): AxumPath<String>,
    headers: HeaderMap,
) -> Response {
    if !authorized(&state, &headers) {
        return json_error(StatusCode::UNAUTHORIZED, "invalid bridge token");
    }
    if let Err(error) = ArtifactStore::validate_sha(&sha) {
        return json_error(StatusCode::BAD_REQUEST, error);
    }
    let cas = state.artifacts.cas(&sha);
    if cas.is_file() {
        let size = match std::fs::metadata(&cas) {
            Ok(value) => value.len(),
            Err(error) => return json_error(StatusCode::INTERNAL_SERVER_ERROR, error.to_string()),
        };
        let artifact = state
            .artifacts
            .read_cas_meta(&sha)
            .map(|meta| meta.artifact)
            .unwrap_or(ArtifactRef {
                sha256: sha.to_ascii_lowercase(),
                size,
                name: sha.to_ascii_lowercase(),
                mime: None,
                source_device: None,
            });
        return Json(ArtifactStatus {
            artifact,
            complete: true,
            received_offsets: Vec::new(),
            chunk_size: state.artifacts.chunk_size,
        })
        .into_response();
    }
    let meta = match state.artifacts.read_meta(&sha) {
        Ok(value) => value,
        Err(error) => return json_error(StatusCode::NOT_FOUND, error),
    };
    let offsets = match state.artifacts.offsets(&sha) {
        Ok(value) => value,
        Err(error) => return json_error(StatusCode::INTERNAL_SERVER_ERROR, error),
    };
    Json(ArtifactStatus {
        artifact: meta.artifact,
        complete: false,
        received_offsets: offsets,
        chunk_size: meta.chunk_size,
    })
    .into_response()
}

pub async fn offer(
    State(state): State<AppState>,
    AxumPath(sha): AxumPath<String>,
    headers: HeaderMap,
    Json(artifact): Json<ArtifactRef>,
) -> Response {
    if !authorized(&state, &headers) {
        return json_error(StatusCode::UNAUTHORIZED, "invalid bridge token");
    }
    if let Err(error) = ArtifactStore::validate_sha(&sha) {
        return json_error(StatusCode::BAD_REQUEST, error);
    }
    if !artifact.sha256.eq_ignore_ascii_case(&sha) {
        return json_error(StatusCode::CONFLICT, "artifact hash does not match route");
    }
    let meta = IncomingMeta {
        artifact,
        chunk_size: state.artifacts.chunk_size,
    };
    let cas = state.artifacts.cas(&sha);
    if cas.is_file() {
        if let Ok(raw) = serde_json::to_vec_pretty(&meta) {
            let _ = std::fs::write(cas.with_extension("json"), raw);
        }
        return Json(json!({"ok": true, "complete": true, "reused": true})).into_response();
    }
    let incoming = state.artifacts.incoming(&sha);
    if let Err(error) = std::fs::create_dir_all(incoming.join("chunks")) {
        return json_error(StatusCode::INTERNAL_SERVER_ERROR, error.to_string());
    }
    let meta_path = incoming.join("meta.json");
    if meta_path.exists() {
        match state.artifacts.read_meta(&sha) {
            Ok(existing) if existing.artifact.size == meta.artifact.size => {}
            Ok(_) => return json_error(StatusCode::CONFLICT, "artifact offer conflict"),
            Err(error) => return json_error(StatusCode::CONFLICT, error),
        }
    } else if let Err(error) = serde_json::to_vec_pretty(&meta)
        .map_err(std::io::Error::other)
        .and_then(|raw| std::fs::write(&meta_path, raw))
    {
        return json_error(StatusCode::INTERNAL_SERVER_ERROR, error.to_string());
    }
    Json(json!({"ok": true, "complete": false})).into_response()
}

pub async fn put_chunk(
    State(state): State<AppState>,
    AxumPath((sha, offset)): AxumPath<(String, u64)>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    if !authorized(&state, &headers) {
        return json_error(StatusCode::UNAUTHORIZED, "invalid bridge token");
    }
    if let Err(error) = ArtifactStore::validate_sha(&sha) {
        return json_error(StatusCode::BAD_REQUEST, error);
    }
    if body.is_empty() || body.len() as u64 > state.artifacts.chunk_size {
        return json_error(StatusCode::PAYLOAD_TOO_LARGE, "invalid artifact chunk size");
    }
    let parse_header = |name: &'static str| -> Result<String, String> {
        headers
            .get(name)
            .and_then(|x| x.to_str().ok())
            .map(str::to_string)
            .ok_or_else(|| format!("missing {name}"))
    };
    let total = match parse_header("x-praxis-total-size").and_then(|x| {
        x.parse::<u64>()
            .map_err(|_| "invalid total size".to_string())
    }) {
        Ok(value) => value,
        Err(error) => return json_error(StatusCode::BAD_REQUEST, error),
    };
    let name = match parse_header("x-praxis-name-hex").and_then(|value| {
        hex::decode(value)
            .map_err(|_| "invalid artifact name encoding".to_string())
            .and_then(|bytes| {
                String::from_utf8(bytes).map_err(|_| "artifact name is not UTF-8".to_string())
            })
    }) {
        Ok(value) => value,
        Err(error) => return json_error(StatusCode::BAD_REQUEST, error),
    };
    let chunk_sha = match parse_header("x-praxis-chunk-sha256") {
        Ok(value) => value,
        Err(error) => return json_error(StatusCode::BAD_REQUEST, error),
    };
    if offset > total || offset.saturating_add(body.len() as u64) > total {
        return json_error(StatusCode::BAD_REQUEST, "chunk is outside artifact size");
    }
    let actual_chunk = hex::encode(Sha256::digest(&body));
    if actual_chunk != chunk_sha.to_ascii_lowercase() {
        return json_error(StatusCode::CONFLICT, "chunk hash mismatch");
    }
    let incoming = state.artifacts.incoming(&sha);
    let chunks = incoming.join("chunks");
    if let Err(error) = std::fs::create_dir_all(&chunks) {
        return json_error(StatusCode::INTERNAL_SERVER_ERROR, error.to_string());
    }
    let meta_path = incoming.join("meta.json");
    if meta_path.exists() {
        match state.artifacts.read_meta(&sha) {
            Ok(meta) if meta.artifact.size == total => {}
            Ok(_) => return json_error(StatusCode::CONFLICT, "artifact size conflict"),
            Err(error) => return json_error(StatusCode::CONFLICT, error),
        }
    } else {
        let meta = IncomingMeta {
            artifact: ArtifactRef {
                sha256: sha.to_ascii_lowercase(),
                size: total,
                name,
                mime: headers
                    .get("x-praxis-mime")
                    .and_then(|x| x.to_str().ok())
                    .map(str::to_string),
                source_device: headers
                    .get("x-praxis-device-id")
                    .and_then(|x| x.to_str().ok())
                    .map(str::to_string),
            },
            chunk_size: state.artifacts.chunk_size,
        };
        let raw = match serde_json::to_vec_pretty(&meta) {
            Ok(value) => value,
            Err(error) => return json_error(StatusCode::INTERNAL_SERVER_ERROR, error.to_string()),
        };
        if let Err(error) = std::fs::write(&meta_path, raw) {
            return json_error(StatusCode::INTERNAL_SERVER_ERROR, error.to_string());
        }
    }
    let final_path = chunks.join(format!("{offset}.chunk"));
    if final_path.is_file() {
        match std::fs::read(&final_path) {
            Ok(existing) if hex::encode(Sha256::digest(&existing)) == actual_chunk => {
                return Json(json!({"ok": true, "sha256": sha, "offset": offset, "size": body.len(), "reused": true})).into_response();
            }
            Ok(_) => return json_error(StatusCode::CONFLICT, "existing chunk hash conflict"),
            Err(error) => return json_error(StatusCode::INTERNAL_SERVER_ERROR, error.to_string()),
        }
    }
    let stage = chunks.join(format!("{offset}.part"));
    if let Err(error) =
        std::fs::write(&stage, &body).and_then(|_| std::fs::rename(&stage, &final_path))
    {
        return json_error(StatusCode::INTERNAL_SERVER_ERROR, error.to_string());
    }
    Json(json!({"ok": true, "sha256": sha, "offset": offset, "size": body.len()})).into_response()
}

pub async fn complete(
    State(state): State<AppState>,
    AxumPath(sha): AxumPath<String>,
    headers: HeaderMap,
) -> Response {
    if !authorized(&state, &headers) {
        return json_error(StatusCode::UNAUTHORIZED, "invalid bridge token");
    }
    if let Err(error) = ArtifactStore::validate_sha(&sha) {
        return json_error(StatusCode::BAD_REQUEST, error);
    }
    let cas = state.artifacts.cas(&sha);
    if cas.is_file() {
        return Json(json!({"ok": true, "sha256": sha, "reused": true})).into_response();
    }
    let meta = match state.artifacts.read_meta(&sha) {
        Ok(value) => value,
        Err(error) => return json_error(StatusCode::NOT_FOUND, error),
    };
    let incoming = state.artifacts.incoming(&sha);
    let mut chunks = BTreeMap::new();
    for offset in match state.artifacts.offsets(&sha) {
        Ok(value) => value,
        Err(error) => return json_error(StatusCode::INTERNAL_SERVER_ERROR, error),
    } {
        chunks.insert(
            offset,
            incoming.join("chunks").join(format!("{offset}.chunk")),
        );
    }
    let parent = match cas.parent() {
        Some(value) => value,
        None => return json_error(StatusCode::INTERNAL_SERVER_ERROR, "invalid CAS path"),
    };
    if let Err(error) = std::fs::create_dir_all(parent) {
        return json_error(StatusCode::INTERNAL_SERVER_ERROR, error.to_string());
    }
    let stage = parent.join(format!(".{}.assembling", sha));
    let assembled = (|| -> Result<(u64, String), String> {
        let mut output = std::fs::File::create(&stage).map_err(|e| e.to_string())?;
        let mut hasher = Sha256::new();
        let mut expected_offset = 0u64;
        for (offset, path) in chunks {
            if offset != expected_offset {
                return Err(format!("missing chunk at offset {expected_offset}"));
            }
            let mut input = std::fs::File::open(path).map_err(|e| e.to_string())?;
            let mut buffer = vec![0u8; state.artifacts.chunk_size as usize];
            loop {
                let read = input.read(&mut buffer).map_err(|e| e.to_string())?;
                if read == 0 {
                    break;
                }
                output
                    .write_all(&buffer[..read])
                    .map_err(|e| e.to_string())?;
                hasher.update(&buffer[..read]);
                expected_offset += read as u64;
            }
        }
        output.sync_all().map_err(|e| e.to_string())?;
        Ok((expected_offset, hex::encode(hasher.finalize())))
    })();
    let (size, actual_sha) = match assembled {
        Ok(value) => value,
        Err(error) => return json_error(StatusCode::CONFLICT, error),
    };
    if size != meta.artifact.size || actual_sha != sha.to_ascii_lowercase() {
        let _ = std::fs::remove_file(&stage);
        return json_error(
            StatusCode::CONFLICT,
            "assembled artifact hash or size mismatch",
        );
    }
    if let Err(error) = std::fs::rename(&stage, &cas) {
        return json_error(StatusCode::INTERNAL_SERVER_ERROR, error.to_string());
    }
    if let Ok(raw) = serde_json::to_vec_pretty(&meta) {
        let _ = std::fs::write(cas.with_extension("json"), raw);
    }
    let _ = std::fs::remove_dir_all(incoming);
    Json(json!({"ok": true, "artifact": meta.artifact})).into_response()
}

pub async fn get_chunk(
    State(state): State<AppState>,
    AxumPath(sha): AxumPath<String>,
    Query(query): Query<DownloadQuery>,
    headers: HeaderMap,
) -> Response {
    if !authorized(&state, &headers) {
        return json_error(StatusCode::UNAUTHORIZED, "invalid bridge token");
    }
    if let Err(error) = ArtifactStore::validate_sha(&sha) {
        return json_error(StatusCode::BAD_REQUEST, error);
    }
    let path = state.artifacts.cas(&sha);
    let size = match std::fs::metadata(&path) {
        Ok(value) => value.len(),
        Err(_) => return json_error(StatusCode::NOT_FOUND, "artifact is not complete"),
    };
    if query.offset > size {
        return json_error(StatusCode::RANGE_NOT_SATISFIABLE, "offset beyond artifact");
    }
    let length = query
        .length
        .unwrap_or(state.artifacts.chunk_size)
        .min(state.artifacts.chunk_size)
        .min(size - query.offset);
    let mut file = match std::fs::File::open(path) {
        Ok(value) => value,
        Err(error) => return json_error(StatusCode::INTERNAL_SERVER_ERROR, error.to_string()),
    };
    if let Err(error) = file.seek(SeekFrom::Start(query.offset)) {
        return json_error(StatusCode::INTERNAL_SERVER_ERROR, error.to_string());
    }
    let mut buffer = vec![0u8; length as usize];
    if let Err(error) = file.read_exact(&mut buffer) {
        return json_error(StatusCode::INTERNAL_SERVER_ERROR, error.to_string());
    }
    let mut response = buffer.into_response();
    response.headers_mut().insert(
        "x-praxis-total-size",
        size.to_string().parse().expect("size header"),
    );
    response.headers_mut().insert(
        "x-praxis-offset",
        query.offset.to_string().parse().expect("offset header"),
    );
    response
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_paths_disguised_as_hashes() {
        assert!(ArtifactStore::validate_sha("../secret").is_err());
        assert!(ArtifactStore::validate_sha(&"a".repeat(64)).is_ok());
    }
}
