use std::fmt;

use base64::Engine;
use serde::{Deserialize, Serialize};
use url::Url;

/// Single source of truth for the model slugs this relay advertises and accepts.
///
/// Both `/v1/models` (via [`supported_model_list`]) and `/v1/chat/completions`
/// (via [`is_supported_model`]) derive from this one list, so the advertised set
/// can never drift from the accepted set. Add a model here and both endpoints
/// follow — no more editing three unrelated places (the drift bug this pass fixes).
// Codex backend 0.144.0 exposes 5.6 as three explicit, User-Agent-gated
// variants. Keep the generic `gpt-5.6` alias out: the isolated canary rejects
// it even when these three identifiers succeed.
pub const SUPPORTED_MODELS: &[&str] = &[
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
];

/// The largest JSON body accepted by the Chat Completions endpoint.
///
/// A 20 MiB image expands to roughly 26.7 MiB when base64 encoded, so 32 MiB
/// leaves room for the surrounding JSON while still bounding relay memory use.
pub const MAX_CHAT_REQUEST_BYTES: usize = 32 * 1024 * 1024;

/// Relay-level limits are intentionally tighter than the upstream API limits.
/// Telegram albums contain at most ten items, so twenty images still leaves
/// headroom for multi-message context without allowing unbounded requests.
pub const MAX_IMAGE_PARTS_PER_REQUEST: usize = 20;
pub const MAX_REMOTE_IMAGE_URL_BYTES: usize = 16 * 1024;
pub const MAX_DATA_IMAGE_BYTES: usize = 20 * 1024 * 1024;
pub const MAX_WEB_SEARCH_USES: u32 = 10;

/// True if `model` is exactly one of the models this relay supports.
///
/// Replaces the old `starts_with("gpt-5")` prefix check, which both let bogus
/// slugs like `"gpt-5-nope"` through (failing later with a confusing error) and
/// would reject any future model not literally prefixed `gpt-5`.
pub fn is_supported_model(model: &str) -> bool {
    SUPPORTED_MODELS.contains(&model)
}

/// Build the `/v1/models` payload from [`SUPPORTED_MODELS`] so the advertised
/// list is generated from the same constant the request validator checks against.
pub fn supported_model_list() -> ModelList {
    let created = chrono::Utc::now().timestamp();
    ModelList {
        object: "list".to_string(),
        data: SUPPORTED_MODELS
            .iter()
            .map(|id| Model {
                id: (*id).to_string(),
                object: "model".to_string(),
                created,
                owned_by: "chatgpt".to_string(),
            })
            .collect(),
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChatRequest {
    pub model: String,
    pub messages: Vec<Message>,
    #[serde(default)]
    pub stream: bool,
    #[serde(default)]
    pub temperature: Option<f64>,
    #[serde(default)]
    pub max_tokens: Option<u32>,
    #[serde(default)]
    pub tools: Vec<Tool>,
    // Optional per-request knobs: reasoning depth and upstream cache affinity.
    // Absent fields fall back to relay-side defaults (RELAY_REASONING_EFFORT env,
    // derived conversation affinity), so old clients keep byte-identical payloads.
    #[serde(default)]
    pub reasoning_effort: Option<String>,
    #[serde(default)]
    pub prompt_cache_key: Option<String>,
}

impl ChatRequest {
    /// Validate the semantic constraints that serde alone cannot express.
    ///
    /// JSON shape errors are rejected by Axum's `Json` extractor. This pass
    /// handles role restrictions, URL schemes, data-URL media/base64 checks,
    /// per-image size, and the request-wide image count.
    pub fn validate_content(&self) -> Result<(), RequestValidationError> {
        let mut web_search_index = None;
        for (tool_index, tool) in self.tools.iter().enumerate() {
            match tool {
                Tool::Function { function } => {
                    let name = function.name.as_str();
                    if name.is_empty()
                        || name.len() > 64
                        || !name
                            .bytes()
                            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
                    {
                        return Err(RequestValidationError::new(
                            format!("tools[{tool_index}].function.name"),
                            "function name must be 1-64 ASCII letters, digits, underscores, or hyphens",
                        ));
                    }
                }
                Tool::WebSearch { max_uses, .. } => {
                    if let Some(previous_index) = web_search_index.replace(tool_index) {
                        return Err(RequestValidationError::new(
                            format!("tools[{tool_index}]"),
                            format!(
                                "only one web_search tool is allowed; first declared at tools[{previous_index}]"
                            ),
                        ));
                    }

                    if let Some(max_uses) = max_uses {
                        if *max_uses == 0 || *max_uses > MAX_WEB_SEARCH_USES {
                            return Err(RequestValidationError::new(
                                format!("tools[{tool_index}].max_uses"),
                                format!("max_uses must be between 1 and {MAX_WEB_SEARCH_USES}"),
                            ));
                        }
                    }
                }
            }
        }

        let mut image_count = 0usize;

        for (message_index, message) in self.messages.iter().enumerate() {
            let Some(MessageContent::Parts(parts)) = &message.content else {
                continue;
            };

            if parts.is_empty() {
                return Err(RequestValidationError::new(
                    format!("messages[{message_index}].content"),
                    "content parts must not be empty",
                ));
            }

            for (part_index, part) in parts.iter().enumerate() {
                let MessageContentPart::ImageUrl { image_url } = part else {
                    continue;
                };

                let param =
                    format!("messages[{message_index}].content[{part_index}].image_url.url");

                if message.role != "user" {
                    return Err(RequestValidationError::new(
                        param,
                        "image_url content parts are only supported for user messages",
                    ));
                }

                image_count += 1;
                if image_count > MAX_IMAGE_PARTS_PER_REQUEST {
                    return Err(RequestValidationError::new(
                        format!("messages[{message_index}].content"),
                        format!(
                            "at most {MAX_IMAGE_PARTS_PER_REQUEST} image_url parts are allowed per request"
                        ),
                    ));
                }

                if self.model == "gpt-5.4-mini" && image_url.detail == Some(ImageDetail::Original) {
                    return Err(RequestValidationError::new(
                        format!("messages[{message_index}].content[{part_index}].image_url.detail"),
                        "detail=original is not supported by gpt-5.4-mini",
                    ));
                }

                validate_image_url(&image_url.url)
                    .map_err(|reason| RequestValidationError::new(param, reason))?;
            }
        }

        Ok(())
    }

    pub fn image_part_count(&self) -> usize {
        self.messages
            .iter()
            .filter_map(|message| match &message.content {
                Some(MessageContent::Parts(parts)) => Some(parts),
                _ => None,
            })
            .flatten()
            .filter(|part| matches!(part, MessageContentPart::ImageUrl { .. }))
            .count()
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Message {
    pub role: String,
    // Optional: an assistant turn that only makes tool calls sends content=null; a non-optional
    // String rejected the whole request with HTTP 422 and broke every tool-loop follow-up turn.
    #[serde(default)]
    pub content: Option<MessageContent>,
    // OpenAI tool-calling fields, needed to replay a tool loop into the Responses API.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_calls: Option<serde_json::Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_call_id: Option<String>,
}

impl Message {
    /// Preserve the legacy string/null behavior for roles whose upstream
    /// representation is textual. Text parts are concatenated in order;
    /// validation guarantees that images cannot appear for these roles.
    pub fn text_content(&self) -> String {
        match &self.content {
            None => String::new(),
            Some(MessageContent::Text(text)) => text.clone(),
            Some(MessageContent::Parts(parts)) => parts
                .iter()
                .filter_map(|part| match part {
                    MessageContentPart::Text { text } => Some(text.as_str()),
                    MessageContentPart::ImageUrl { .. } => None,
                })
                .collect(),
        }
    }
}

/// Chat Completions accepts either the historical string form or a typed
/// content array. `Option<MessageContent>` on [`Message`] supplies the `null`
/// variant without changing serialization of existing clients.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(untagged)]
pub enum MessageContent {
    Text(String),
    Parts(Vec<MessageContentPart>),
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(tag = "type", deny_unknown_fields)]
pub enum MessageContentPart {
    #[serde(rename = "text")]
    Text { text: String },
    #[serde(rename = "image_url")]
    ImageUrl { image_url: ImageUrlContent },
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ImageUrlContent {
    pub url: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub detail: Option<ImageDetail>,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum ImageDetail {
    Auto,
    Low,
    High,
    Original,
}

impl ImageDetail {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Auto => "auto",
            Self::Low => "low",
            Self::High => "high",
            Self::Original => "original",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RequestValidationError {
    pub param: String,
    pub message: String,
}

impl RequestValidationError {
    fn new(param: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            param: param.into(),
            message: message.into(),
        }
    }
}

impl fmt::Display for RequestValidationError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: {}", self.param, self.message)
    }
}

impl std::error::Error for RequestValidationError {}

fn validate_image_url(image_url: &str) -> Result<(), String> {
    if image_url.starts_with("data:") {
        return validate_data_image_url(image_url, MAX_DATA_IMAGE_BYTES);
    }

    if image_url.len() > MAX_REMOTE_IMAGE_URL_BYTES {
        return Err(format!(
            "remote image URL exceeds the {MAX_REMOTE_IMAGE_URL_BYTES}-byte limit"
        ));
    }

    let parsed = Url::parse(image_url)
        .map_err(|_| "image URL must be an absolute HTTPS URL or a supported data URL")?;

    if parsed.scheme() != "https" {
        return Err("remote image URL must use https".to_string());
    }
    if parsed.host_str().is_none() {
        return Err("remote image URL must include a host".to_string());
    }
    if !parsed.username().is_empty() || parsed.password().is_some() {
        return Err("remote image URL must not contain credentials".to_string());
    }

    Ok(())
}

fn validate_data_image_url(image_url: &str, max_decoded_bytes: usize) -> Result<(), String> {
    let (header, encoded) = image_url
        .strip_prefix("data:")
        .and_then(|rest| rest.split_once(','))
        .ok_or_else(|| {
            "data URL must use data:image/<png|jpeg|webp|gif>;base64,<payload>".to_string()
        })?;

    let mime = match header {
        "image/png;base64" => "image/png",
        "image/jpeg;base64" => "image/jpeg",
        "image/webp;base64" => "image/webp",
        "image/gif;base64" => "image/gif",
        _ => return Err(
            "data URL MIME must be image/png, image/jpeg, image/webp, or image/gif and use base64"
                .to_string(),
        ),
    };

    if encoded.is_empty() {
        return Err("data URL payload must not be empty".to_string());
    }

    // Reject oversized inputs before allocating a decoded buffer. The second
    // check below handles padding and keeps the cap exact.
    let max_encoded_bytes = 4 * max_decoded_bytes.div_ceil(3);
    if encoded.len() > max_encoded_bytes {
        return Err(format!(
            "decoded data URL image exceeds the {max_decoded_bytes}-byte limit"
        ));
    }

    let decoded = base64::engine::general_purpose::STANDARD
        .decode(encoded)
        .map_err(|_| "data URL contains invalid base64".to_string())?;

    if decoded.len() > max_decoded_bytes {
        return Err(format!(
            "decoded data URL image exceeds the {max_decoded_bytes}-byte limit"
        ));
    }

    let signature_matches = match mime {
        "image/png" => decoded.starts_with(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg" => decoded.starts_with(&[0xff, 0xd8, 0xff]),
        "image/gif" => decoded.starts_with(b"GIF87a") || decoded.starts_with(b"GIF89a"),
        "image/webp" => {
            decoded.len() >= 12 && decoded.starts_with(b"RIFF") && &decoded[8..12] == b"WEBP"
        }
        _ => false,
    };

    if !signature_matches {
        return Err(format!("data URL bytes do not match declared MIME {mime}"));
    }

    Ok(())
}

/// Strict relay input union. Hosted search remains a Responses tool and must
/// never be surfaced to clients as a function call they are expected to run.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum Tool {
    Function {
        function: Function,
    },
    WebSearch {
        /// Relay-only compatibility field. The ChatGPT Codex backend rejects
        /// top-level `max_tool_calls`, so this is validated locally and never
        /// serialized upstream.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        max_uses: Option<u32>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        external_web_access: Option<bool>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        search_context_size: Option<WebSearchContextSize>,
    },
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Function {
    pub name: String,
    pub description: Option<String>,
    pub parameters: serde_json::Value,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub strict: Option<bool>,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum WebSearchContextSize {
    Low,
    Medium,
    High,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChatResponse {
    pub id: String,
    pub object: String,
    pub created: i64,
    pub model: String,
    pub choices: Vec<Choice>,
    pub usage: Usage,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Choice {
    pub index: u32,
    pub message: Message,
    pub finish_reason: String,
}

/// Cached prefix accounting, OpenAI-compatible shape.
///
/// 02.08.2026. Кэш на этом пути АВТОМАТИЧЕСКИЙ: провайдер сам кэширует совпадающий
/// префикс, его нельзя «включить» — его зарабатывают стабильностью байтов. Отчитывается
/// он единственным способом, вот этим полем. Реле схлопывало usage до
/// `{prompt_tokens, completion_tokens, total_tokens}` и деталь теряло — поэтому у Praxis
/// в учёте стояли нули по всем ходам через gpt, и это читалось как «кэш не работает».
/// На самом деле это означало «не сообщено»: отличить одно от другого было нечем.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PromptTokensDetails {
    pub cached_tokens: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Usage {
    pub prompt_tokens: u32,
    pub completion_tokens: u32,
    pub total_tokens: u32,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub prompt_tokens_details: Option<PromptTokensDetails>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Model {
    pub id: String,
    pub object: String,
    pub created: i64,
    pub owned_by: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ModelList {
    pub object: String,
    pub data: Vec<Model>,
}

// Response events for streaming
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ResponseEvent {
    pub id: String,
    pub object: String,
    pub created: i64,
    pub model: String,
    pub choices: Vec<ResponseChoice>,
    // Token accounting from response.completed, forwarded so the client's cost
    // meter is not blind (reasoning tokens are included in completion_tokens).
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub usage: Option<Usage>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ResponseChoice {
    pub index: u32,
    pub delta: ResponseDelta,
    pub finish_reason: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ResponseDelta {
    pub role: Option<String>,
    pub content: Option<String>,
    #[serde(rename = "tool_calls")]
    pub tool_calls: Option<serde_json::Value>,
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn request_with(model: &str, role: &str, content: serde_json::Value) -> ChatRequest {
        serde_json::from_value(json!({
            "model": model,
            "messages": [{
                "role": role,
                "content": content
            }]
        }))
        .expect("request fixture should deserialize")
    }

    fn data_url(mime: &str, bytes: &[u8]) -> String {
        format!(
            "data:{mime};base64,{}",
            base64::engine::general_purpose::STANDARD.encode(bytes)
        )
    }

    #[test]
    fn valid_models_are_accepted() {
        // Every advertised model must pass the request validator.
        for m in SUPPORTED_MODELS {
            assert!(is_supported_model(m), "{m} should be supported");
        }
        assert!(!is_supported_model("gpt-5.6"));
    }

    #[test]
    fn invalid_models_are_rejected() {
        // Bogus slugs, and — crucially — a bare "gpt-5" prefix that the old
        // starts_with() check would have wrongly let through.
        for m in [
            "gpt-4",
            "gpt-5-nonexistent",
            "gpt-5",
            "gpt-5.4-turbo",
            "",
            "GPT-5.4",
        ] {
            assert!(!is_supported_model(m), "{m:?} should NOT be supported");
        }
    }

    #[test]
    fn models_endpoint_matches_supported_list() {
        // /v1/models must return exactly SUPPORTED_MODELS, in order — the
        // guard against the advertised list drifting from the accepted list.
        let list = supported_model_list();
        assert_eq!(list.object, "list");
        let ids: Vec<&str> = list.data.iter().map(|m| m.id.as_str()).collect();
        assert_eq!(ids, SUPPORTED_MODELS.to_vec());
        for m in &list.data {
            assert_eq!(m.object, "model");
            assert_eq!(m.owned_by, "chatgpt");
        }
    }

    #[test]
    fn message_content_keeps_string_null_and_typed_parts_contract() {
        let request: ChatRequest = serde_json::from_value(json!({
            "model": "gpt-5.5",
            "messages": [
                {"role": "user", "content": "legacy"},
                {"role": "assistant", "content": null},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "look"},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "https://example.com/image.png",
                                "detail": "original"
                            }
                        }
                    ]
                }
            ]
        }))
        .unwrap();

        assert_eq!(
            request.messages[0].content,
            Some(MessageContent::Text("legacy".to_string()))
        );
        assert_eq!(request.messages[1].content, None);
        assert!(matches!(
            request.messages[2].content,
            Some(MessageContent::Parts(_))
        ));
        request.validate_content().unwrap();

        let round_trip = serde_json::to_value(&request).unwrap();
        assert_eq!(round_trip["messages"][0]["content"], json!("legacy"));
        assert!(round_trip["messages"][1]["content"].is_null());
        assert!(round_trip["messages"][2]["content"].is_array());
    }

    #[test]
    fn strict_part_schema_rejects_unknown_types_fields_and_details() {
        for content in [
            json!([{"type": "audio_url", "audio_url": {"url": "https://example.com/a"}}]),
            json!([{"type": "text", "text": "ok", "extra": true}]),
            json!([{
                "type": "image_url",
                "image_url": {"url": "https://example.com/a.png", "detail": "ultra"}
            }]),
        ] {
            let value = json!({
                "model": "gpt-5.5",
                "messages": [{"role": "user", "content": content}]
            });
            assert!(serde_json::from_value::<ChatRequest>(value).is_err());
        }
    }

    #[test]
    fn strict_tool_union_accepts_functions_and_hosted_search_only() {
        let request: ChatRequest = serde_json::from_value(json!({
            "model": "gpt-5.5",
            "messages": [{"role": "user", "content": "find it"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "web_read",
                        "description": "Read a page",
                        "parameters": {"type": "object"},
                        "strict": true
                    }
                },
                {
                    "type": "web_search",
                    "external_web_access": true,
                    "search_context_size": "medium",
                    "max_uses": 3
                }
            ]
        }))
        .unwrap();

        request.validate_content().unwrap();
        assert!(matches!(request.tools[0], Tool::Function { .. }));
        assert!(matches!(request.tools[1], Tool::WebSearch { .. }));
    }

    #[test]
    fn strict_tool_union_rejects_unknown_types_and_fields() {
        for tool in [
            json!({"type": "computer_use"}),
            json!({"type": "web_search", "external_web_access": true, "extra": true}),
            json!({"type": "web_search", "filters": {"allowed_domains": ["example.com"]}}),
            json!({
                "type": "function",
                "function": {
                    "name": "lookup",
                    "parameters": {"type": "object"},
                    "extra": true
                }
            }),
        ] {
            let value = json!({
                "model": "gpt-5.5",
                "messages": [{"role": "user", "content": "test"}],
                "tools": [tool]
            });
            assert!(serde_json::from_value::<ChatRequest>(value).is_err());
        }
    }

    #[test]
    fn hosted_search_limits_are_validated_locally() {
        for invalid in [0, MAX_WEB_SEARCH_USES + 1] {
            let request: ChatRequest = serde_json::from_value(json!({
                "model": "gpt-5.5",
                "messages": [{"role": "user", "content": "test"}],
                "tools": [{"type": "web_search", "max_uses": invalid}]
            }))
            .unwrap();
            assert!(request.validate_content().is_err());
        }

        let duplicate: ChatRequest = serde_json::from_value(json!({
            "model": "gpt-5.5",
            "messages": [{"role": "user", "content": "test"}],
            "tools": [{"type": "web_search"}, {"type": "web_search"}]
        }))
        .unwrap();
        assert!(duplicate.validate_content().is_err());
    }

    #[test]
    fn accepts_https_and_supported_base64_image_types() {
        let urls = [
            "https://example.com/no-extension".to_string(),
            data_url("image/png", b"\x89PNG\r\n\x1a\n"),
            data_url("image/jpeg", &[0xff, 0xd8, 0xff, 0x00]),
            data_url("image/gif", b"GIF89a"),
            data_url("image/webp", b"RIFF\0\0\0\0WEBP"),
        ];

        let parts: Vec<_> = urls
            .into_iter()
            .map(|url| {
                json!({
                    "type": "image_url",
                    "image_url": {"url": url, "detail": "auto"}
                })
            })
            .collect();
        let request = request_with("gpt-5.5", "user", json!(parts));

        request.validate_content().unwrap();
        assert_eq!(request.image_part_count(), 5);
    }

    #[test]
    fn rejects_unsafe_or_non_absolute_remote_urls() {
        for (url, expected) in [
            ("http://example.com/a.png", "must use https"),
            ("/relative/a.png", "absolute HTTPS URL"),
            (
                "https://user:password@example.com/a.png",
                "must not contain credentials",
            ),
        ] {
            let request = request_with(
                "gpt-5.5",
                "user",
                json!([{"type": "image_url", "image_url": {"url": url}}]),
            );
            let error = request.validate_content().unwrap_err();
            assert!(error.message.contains(expected), "{error}");
        }
    }

    #[test]
    fn rejects_invalid_data_url_mime_base64_and_signature() {
        for (url, expected) in [
            ("data:image/svg+xml;base64,PHN2Zz4=", "MIME"),
            ("data:image/png;base64,***", "invalid base64"),
            (
                "data:image/png;base64,R0lGODlh",
                "do not match declared MIME",
            ),
        ] {
            let request = request_with(
                "gpt-5.5",
                "user",
                json!([{"type": "image_url", "image_url": {"url": url}}]),
            );
            let error = request.validate_content().unwrap_err();
            assert!(error.message.contains(expected), "{error}");
        }
    }

    #[test]
    fn data_url_decoded_size_cap_is_enforced_before_upstream() {
        let url = data_url("image/png", b"\x89PNG\r\n\x1a\n");
        let error = validate_data_image_url(&url, 7).unwrap_err();
        assert!(error.contains("7-byte limit"));
    }

    #[test]
    fn request_image_count_cap_is_enforced() {
        let parts: Vec<_> = (0..=MAX_IMAGE_PARTS_PER_REQUEST)
            .map(|index| {
                json!({
                    "type": "image_url",
                    "image_url": {"url": format!("https://example.com/{index}.png")}
                })
            })
            .collect();
        let request = request_with("gpt-5.5", "user", json!(parts));

        let error = request.validate_content().unwrap_err();
        assert!(error
            .message
            .contains(&MAX_IMAGE_PARTS_PER_REQUEST.to_string()));
    }

    #[test]
    fn images_are_user_only_and_original_is_model_aware() {
        let image = json!([{
            "type": "image_url",
            "image_url": {
                "url": "https://example.com/a.png",
                "detail": "original"
            }
        }]);

        let assistant = request_with("gpt-5.5", "assistant", image.clone());
        assert!(assistant
            .validate_content()
            .unwrap_err()
            .message
            .contains("only supported for user"));

        let mini = request_with("gpt-5.4-mini", "user", image);
        assert!(mini
            .validate_content()
            .unwrap_err()
            .message
            .contains("not supported by gpt-5.4-mini"));
    }

    #[test]
    fn empty_content_parts_are_rejected() {
        let request = request_with("gpt-5.5", "user", json!([]));
        assert!(request
            .validate_content()
            .unwrap_err()
            .message
            .contains("must not be empty"));
    }
}
