use anyhow::Result;
use futures_util::StreamExt;
use reqwest::{Client, RequestBuilder};
use serde_json::{json, Value};
use std::collections::HashSet;
use tokio::sync::mpsc;
use url::Url;

use crate::core::account_router::AccountRouter;
use crate::core::config::Config;
use crate::core::models::{
    ChatRequest, ImageUrlContent, Message, MessageContent, MessageContentPart, ResponseChoice,
    ResponseDelta, ResponseEvent, Tool,
};

const MAX_VISIBLE_CITATIONS: usize = 12;
const MAX_UPSTREAM_ERROR_CHARS: usize = 500;
const CHATGPT_CODEX_RESPONSES_URL: &str = "https://chatgpt.com/backend-api/codex/responses";
const CODEX_CLI_VERSION: &str = "0.144.0";
const CODEX_ORIGINATOR: &str = "codex_cli_rs";
const CODEX_USER_AGENT: &str = "codex_cli_rs/0.144.0";
// Efforts the Responses API can accept; anything else is dropped rather than sent.
const REASONING_EFFORTS: &[&str] = &["none", "minimal", "low", "medium", "high", "xhigh"];

/// Decode one SSE chunk, carrying an *incomplete* trailing UTF-8 sequence over to the
/// next chunk instead of mangling it.
///
/// A multi-byte character (any Cyrillic letter is two bytes) can be split across a
/// chunk boundary. The previous code ran `from_utf8_lossy` per chunk, so the split
/// character became two U+FFFD — silently rewriting the model's words on the way out,
/// ~100 times an hour on a Russian-speaking client. The carry is a valid prefix of a
/// UTF-8 sequence, so it is at most 3 bytes and cannot grow.
///
/// Genuinely invalid bytes (`error_len() == Some`) are still replaced with U+FFFD and
/// reported — that is a real upstream defect, not a boundary artifact.
fn decode_stream_chunk(bytes: &[u8], carry: &mut Vec<u8>) -> String {
    let mut pending = std::mem::take(carry);
    pending.extend_from_slice(bytes);

    let mut out = String::with_capacity(pending.len());
    let mut rest: &[u8] = &pending;
    loop {
        match std::str::from_utf8(rest) {
            Ok(s) => {
                out.push_str(s);
                break;
            }
            Err(e) => {
                let valid_up_to = e.valid_up_to();
                if let Ok(s) = std::str::from_utf8(&rest[..valid_up_to]) {
                    out.push_str(s);
                }
                match e.error_len() {
                    // Truncated tail: hold it back, the rest of the character is in the next chunk.
                    None => {
                        carry.extend_from_slice(&rest[valid_up_to..]);
                        break;
                    }
                    // Actually broken bytes: mark and continue past them.
                    Some(bad) => {
                        println!("⚠️  UTF-8 error: {}, dropping {} invalid byte(s)", e, bad);
                        out.push('\u{FFFD}');
                        rest = &rest[valid_up_to + bad..];
                    }
                }
            }
        }
    }
    out
}

// ~60 words instead of ~5k tokens of Codex-CLI coding-agent instructions per call.
// The client's real system prompt travels in the input as a <system> user message;
// this stub only anchors that contract.  Used when RELAY_INSTRUCTIONS=minimal, with
// an automatic per-request retry on the full prompt if upstream rejects it.
const MINIMAL_INSTRUCTIONS: &str = "You are an assistant served through a local relay. \
The conversation's authoritative instructions arrive inside the input as user messages \
wrapped in <system> tags; follow them faithfully. Use the provided tools when they help. \
Reply in the language of the conversation.";

/// Stable per-conversation affinity id: hash of model + first message.  Within one
/// tool loop the first (system) message is byte-stable, so every iteration of a
/// burst shares the same session_id/prompt_cache_key and upstream prompt caching
/// can actually hit; a fresh UUID per request defeated it entirely.
fn conversation_affinity(request: &ChatRequest) -> String {
    use std::hash::{Hash, Hasher};
    let head = request
        .messages
        .first()
        .and_then(|first| serde_json::to_string(first).ok())
        .unwrap_or_default();
    let mut halves = [0u64; 2];
    for (index, half) in halves.iter_mut().enumerate() {
        let mut hasher = std::collections::hash_map::DefaultHasher::new();
        index.hash(&mut hasher);
        request.model.hash(&mut hasher);
        head.hash(&mut hasher);
        *half = hasher.finish();
    }
    let bytes: Vec<u8> = halves
        .iter()
        .flat_map(|half| half.to_be_bytes())
        .collect();
    format!(
        "{:02x}{:02x}{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}{:02x}{:02x}{:02x}{:02x}",
        bytes[0], bytes[1], bytes[2], bytes[3], bytes[4], bytes[5], bytes[6], bytes[7],
        bytes[8], bytes[9], bytes[10], bytes[11], bytes[12], bytes[13], bytes[14], bytes[15]
    )
}

fn responses_message_content(message: &Message) -> Value {
    match &message.content {
        None => Value::String(String::new()),
        Some(MessageContent::Text(text)) => Value::String(text.clone()),
        Some(MessageContent::Parts(parts)) => Value::Array(
            parts
                .iter()
                .map(|part| match part {
                    MessageContentPart::Text { text } => json!({
                        "type": "input_text",
                        "text": text,
                    }),
                    MessageContentPart::ImageUrl {
                        image_url: ImageUrlContent { url, detail },
                    } => {
                        let mut translated = json!({
                            "type": "input_image",
                            "image_url": url,
                        });
                        if let Some(detail) = detail {
                            translated["detail"] = json!(detail.as_str());
                        }
                        translated
                    }
                })
                .collect(),
        ),
    }
}

/// Convert accepted Chat Completions history into Responses input items.
/// The HTTP handler validates every content part before this function is used.
fn build_responses_input(messages: &[Message]) -> Vec<Value> {
    let mut input_messages = Vec::new();

    for msg in messages {
        let text_content = msg.text_content();
        match msg.role.as_str() {
            "system" => {
                input_messages.push(json!({
                    "role": "user",
                    "content": format!("<system>\n{}\n</system>", text_content)
                }));
            }
            "tool" => {
                // Responses API: a tool result is a function_call_output item linked by call_id
                // (not free text). Fall back to the old text wrap only if no id is present.
                match &msg.tool_call_id {
                    Some(call_id) => input_messages.push(json!({
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": text_content
                    })),
                    None => input_messages.push(json!({
                        "role": "assistant",
                        "content": format!("<tool_response>\n{}\n</tool_response>", text_content)
                    })),
                }
            }
            "assistant" => {
                // Assistant text (if any) as a message, then each tool call as a function_call
                // item - the backend needs the full function-calling turn to continue a tool loop.
                if !text_content.is_empty() {
                    input_messages.push(json!({ "role": "assistant", "content": text_content }));
                }
                if let Some(calls) = msg.tool_calls.as_ref().and_then(|v| v.as_array()) {
                    for tc in calls {
                        let call_id = tc.get("id").and_then(|v| v.as_str()).unwrap_or("");
                        let func = tc.get("function");
                        let name = func
                            .and_then(|f| f.get("name"))
                            .and_then(|v| v.as_str())
                            .unwrap_or("");
                        let args = func
                            .and_then(|f| f.get("arguments"))
                            .and_then(|v| v.as_str())
                            .unwrap_or("{}");
                        input_messages.push(json!({
                            "type": "function_call",
                            "call_id": call_id,
                            "name": name,
                            "arguments": args
                        }));
                    }
                }
            }
            "user" | "developer" => {
                input_messages.push(json!({
                    "role": msg.role,
                    "content": responses_message_content(msg)
                }));
            }
            _ => {
                input_messages.push(json!({
                    "role": "user",
                    "content": format!("<{}>\n{}\n</{}>", msg.role, text_content, msg.role)
                }));
            }
        }
    }

    input_messages
}

fn map_tools_for_responses(tools: &[Tool]) -> Vec<Value> {
    tools
        .iter()
        .map(|tool| match tool {
            Tool::Function { function } => {
                let mut mapped = json!({
                    "type": "function",
                    "name": function.name,
                    "parameters": function.parameters,
                    "strict": function.strict.unwrap_or(true),
                });
                if let Some(description) = &function.description {
                    mapped["description"] = json!(description);
                }
                mapped
            }
            Tool::WebSearch {
                external_web_access,
                search_context_size,
                max_uses: _,
            } => {
                let mut mapped = json!({
                    "type": "web_search",
                    "external_web_access": external_web_access.unwrap_or(true),
                });
                if let Some(search_context_size) = search_context_size {
                    mapped["search_context_size"] = json!(search_context_size);
                }
                mapped
            }
        })
        .collect()
}

fn build_responses_payload(
    request: &ChatRequest,
    instructions: String,
    input_messages: Vec<Value>,
    reasoning_effort: Option<&str>,
    prompt_cache_key: Option<&str>,
    parallel_tool_calls: bool,
) -> Value {
    let mapped_tools = map_tools_for_responses(&request.tools);
    let mut payload = json!({
        "model": request.model,
        "instructions": instructions,
        "input": input_messages,
        "store": false,
        "stream": true,
    });
    // GPT-5.6 defaults to medium when omitted; Praxis wants reasoning disabled by
    // default but the knob stays per-request so she can raise depth deliberately.
    if let Some(effort) = reasoning_effort {
        if REASONING_EFFORTS.contains(&effort) {
            payload["reasoning"] = json!({"effort": effort});
        }
    }
    if let Some(key) = prompt_cache_key {
        payload["prompt_cache_key"] = json!(key);
    }

    if !mapped_tools.is_empty() {
        payload["tools"] = json!(mapped_tools);
        payload["tool_choice"] = json!("auto");
        // true lets the model batch several tool calls per response; the translator
        // assigns incrementing indexes and the client executes the whole batch.
        payload["parallel_tool_calls"] = json!(parallel_tool_calls);
    }
    payload
}

fn build_codex_request(
    client: &Client,
    access_token: &str,
    account_id: &str,
    session_id: &str,
    payload: &Value,
) -> RequestBuilder {
    client
        .post(CHATGPT_CODEX_RESPONSES_URL)
        .bearer_auth(access_token)
        .header("chatgpt-account-id", account_id)
        .header(reqwest::header::CONTENT_TYPE, "application/json")
        .header(reqwest::header::ACCEPT, "text/event-stream")
        .header("OpenAI-Beta", "responses=experimental")
        .header("session_id", session_id)
        .header("originator", CODEX_ORIGINATOR)
        .header(reqwest::header::USER_AGENT, CODEX_USER_AGENT)
        .json(payload)
}

pub async fn stream_chat_completions(
    config: &Config,
    account_router: AccountRouter,
    request: ChatRequest,
    client: Client,
) -> Result<mpsc::Receiver<Result<ResponseEvent>>> {
    let (tx, rx) = mpsc::channel(100);
    let config = config.clone();

    tokio::spawn(async move {
        // SOLUTION: Convert system messages to user messages with special formatting
        // ChatGPT Responses API has strict validation on instructions field
        // So we put system messages in the input array as user messages

        let input_messages = build_responses_input(&request.messages);
        let image_part_count = request.image_part_count();

        // Use the full base instructions from prompt.md
        use crate::core::client_common::BASE_INSTRUCTIONS;

        let minimal = config.minimal_instructions();
        let mut full_instructions = BASE_INSTRUCTIONS.to_string();

        // Add user instructions from AGENTS.md if available
        if let Some(user_instructions) = &config.user_instructions {
            full_instructions.push_str("\n\n<user_instructions>\n\n");
            full_instructions.push_str(user_instructions);
            full_instructions.push_str("\n\n</user_instructions>");
        }
        let instructions = if minimal {
            MINIMAL_INSTRUCTIONS.to_string()
        } else {
            full_instructions.clone()
        };

        println!("🔍 DEBUG - Processing {} messages", request.messages.len());
        println!(
            "🔍 DEBUG - Instructions length: {} characters",
            instructions.len()
        );
        println!("🔍 DEBUG - Input messages: {}", input_messages.len());
        println!("🔍 DEBUG - Image parts: {}", image_part_count);

        println!("🔍 DEBUG - Tools in request: {}", request.tools.len());
        let effort_owned = request
            .reasoning_effort
            .clone()
            .or_else(|| config.reasoning_effort.clone());
        let effort = effort_owned
            .as_deref()
            .filter(|value| REASONING_EFFORTS.contains(value));
        let cache_key = request
            .prompt_cache_key
            .clone()
            .unwrap_or_else(|| conversation_affinity(&request));
        println!("🔍 DEBUG - Reasoning effort: {:?}", effort);
        let mut payload = build_responses_payload(
            &request,
            instructions,
            input_messages,
            effort,
            Some(cache_key.as_str()),
            config.parallel_tool_calls,
        );
        let mapped_tool_count = payload
            .get("tools")
            .and_then(Value::as_array)
            .map_or(0, Vec::len);
        println!("🔍 DEBUG - Mapped tools included: {}", mapped_tool_count);
        println!("🔍 DEBUG - Has valid tools: {}", mapped_tool_count > 0);
        // Never log the full payload: multimodal requests may contain private
        // image URLs or megabytes of base64-encoded image data.
        println!("🔍 DEBUG - Request payload prepared (content redacted)");

        // Token and account id come from one immutable account snapshot.  The
        // old pair of independent auth reads could mix identities during a
        // refresh or account change.
        let mut account = match account_router.lease_active().await {
            Ok(account) => account,
            Err(error) => {
                let _ = tx
                    .send(Err(anyhow::anyhow!(
                        "Active subscription authentication failed: {}",
                        error
                    )))
                    .await;
                return;
            }
        };
        println!("Active subscription slot: {}", account.slot);

        // Try the exact URL that working codex uses: base + codex + responses
        println!(
            "🌐 Making request to ChatGPT Responses API: {} (client {})",
            CHATGPT_CODEX_RESPONSES_URL, CODEX_CLI_VERSION
        );

        // CRITICAL: Use exact headers for ChatGPT Plus plan.  The session id is the
        // derived conversation affinity, not a fresh UUID: stable within a tool
        // loop so upstream prompt-cache/routing affinity survives the burst.
        let session_id = cache_key.clone();
        println!("🔍 DEBUG - Auth and session headers prepared (redacted)");

        let mut retried = false;
        let mut account_failover_done = false;
        let response = loop {
            match build_codex_request(
                &client,
                &account.access_token,
                &account.account_id,
                &session_id,
                &payload,
            )
            .send()
            .await
            {
                Ok(resp) => {
                    println!("✅ Got response with status: {}", resp.status());

                    if resp.status().is_success() {
                        break resp;
                    }
                    // Keep the raw body in memory only long enough to derive a
                    // bounded client-safe summary. Never log or return the
                    // complete upstream JSON.
                    let status = resp.status();
                    let response_body = resp
                        .text()
                        .await
                        .unwrap_or_else(|_| "Failed to read response body".to_string());
                    println!("❌ Failed with status: {}", status);
                    println!("🔍 DEBUG - Upstream error body redacted from logs");

                    // A subscription exhaustion response arrives before a
                    // successful SSE stream begins, so replaying the same
                    // request on the standby cannot duplicate text or tools.
                    // Generic 429s are intentionally excluded: only the
                    // explicit quota code may move the whole relay.
                    if subscription_quota_error(status, &response_body)
                        && !account_failover_done
                    {
                        match account_router.switch_after_quota(&account).await {
                            Ok(standby) => {
                                println!(
                                    "Subscription exhausted; retrying on active slot {}",
                                    standby.slot
                                );
                                account = standby;
                                account_failover_done = true;
                                continue;
                            }
                            Err(error) => {
                                println!("Subscription failover unavailable: {}", error);
                            }
                        }
                    }

                    // Optional knobs (reasoning, prompt_cache_key, minimal instructions)
                    // may be rejected by an upstream quirk; retry once in the maximally
                    // conservative shape so the worst case equals the pre-knob relay.
                    if matches!(status.as_u16(), 400 | 404 | 422) && !retried {
                        retried = true;
                        if let Some(object) = payload.as_object_mut() {
                            object.remove("reasoning");
                            object.remove("prompt_cache_key");
                            if minimal {
                                object.insert(
                                    "instructions".to_string(),
                                    serde_json::json!(full_instructions.clone()),
                                );
                            }
                        }
                        println!(
                            "⚠️  Upstream {} — retrying once in conservative shape (no knobs, full instructions)",
                            status
                        );
                        continue;
                    }

                    // Send properly formatted error response as SSE
                    // Transform specific error messages for better user experience
                    let user_friendly_message = if subscription_quota_error(status, &response_body) {
                        "Both OpenAI subscriptions are currently unavailable because of usage limits."
                            .to_string()
                    } else {
                        upstream_error_message(status, &response_body)
                    };

                    let error_event = ResponseEvent {
                        id: format!("error-{}", uuid::Uuid::new_v4()),
                        object: "chat.completion.chunk".to_string(),
                        created: std::time::SystemTime::now()
                            .duration_since(std::time::UNIX_EPOCH)
                            .unwrap_or_default()
                            .as_secs() as i64,
                        model: request.model.clone(),
                        usage: None,
                        choices: vec![ResponseChoice {
                            index: 0,
                            delta: ResponseDelta {
                                role: Some("assistant".to_string()),
                                content: Some(user_friendly_message),
                                tool_calls: None,
                            },
                            finish_reason: Some("error".to_string()),
                        }],
                    };
                    let _ = tx.send(Ok(error_event)).await;
                    return;
                }
                Err(e) => {
                    println!("❌ Request failed: {}", e);
                    let _ = tx
                        .send(Err(anyhow::anyhow!("Request failed: {}", e)))
                        .await;
                    return;
                }
            }
        };

        // Handle streaming response with proper SSE buffering
        let mut stream = response.bytes_stream();
        let mut buffer = String::new();
        // Holds a character split across a chunk boundary (see decode_stream_chunk).
        let mut utf8_carry: Vec<u8> = Vec::new();

        // Deduplication: Track last sent content
        let mut last_sent_content: Option<String> = None;
        let mut tool_call_index: usize = 0;

        while let Some(chunk) = stream.next().await {
            let chunk = match chunk {
                Ok(chunk) => chunk,
                Err(e) => {
                    let error_event = ResponseEvent {
                        id: format!("error-{}", uuid::Uuid::new_v4()),
                        object: "chat.completion.chunk".to_string(),
                        created: std::time::SystemTime::now()
                            .duration_since(std::time::UNIX_EPOCH)
                            .unwrap_or_default()
                            .as_secs() as i64,
                        model: request.model.clone(),
                        usage: None,
                        choices: vec![ResponseChoice {
                            index: 0,
                            delta: ResponseDelta {
                                role: Some("assistant".to_string()),
                                content: Some(format!("Stream error: {}", e)),
                                tool_calls: None,
                            },
                            finish_reason: Some("error".to_string()),
                        }],
                    };
                    let _ = tx.send(Ok(error_event)).await;
                    break;
                }
            };

            let chunk_str = decode_stream_chunk(&chunk, &mut utf8_carry);

            // Add chunk to buffer
            buffer.push_str(&chunk_str);

            // Process complete lines from buffer
            while let Some(line_end) = buffer.find('\n') {
                let line = buffer[..line_end].trim_end_matches('\r').to_string();
                buffer = buffer[line_end + 1..].to_string();

                // Skip empty lines (SSE format requirement)
                if line.is_empty() {
                    continue;
                }

                // Process SSE data lines
                if line.starts_with("data: ") {
                    let json_str = line[6..].trim(); // Remove "data: " prefix

                    // Skip "[DONE]" marker
                    if json_str == "[DONE]" {
                        println!("🏁 Received [DONE] marker, ending stream");
                        return;
                    }

                    // Skip empty data lines
                    if json_str.is_empty() {
                        continue;
                    }

                    match serde_json::from_str::<Value>(json_str) {
                        Ok(event_json) => {
                            println!("📡 SSE event type: {}", safe_event_type(&event_json));
                            // Convert to our ResponseEvent format
                            if let Some(response_event) = parse_sse_event(&event_json, tool_call_index) {
                                if response_event
                                    .choices
                                    .get(0)
                                    .and_then(|choice| choice.delta.tool_calls.as_ref())
                                    .is_some()
                                {
                                    tool_call_index += 1;
                                }
                                // Deduplication logic
                                let mut should_send = true;
                                // Try to extract content from the event
                                let content = response_event
                                    .choices
                                    .get(0)
                                    .and_then(|choice| choice.delta.content.as_ref())
                                    .map(|s| s.trim().to_string());
                                // Only deduplicate non-empty content messages
                                if let Some(ref new_content) = content {
                                    if let Some(ref last_content) = last_sent_content {
                                        if !new_content.is_empty() && new_content == last_content {
                                            should_send = false;
                                        }
                                    }
                                }
                                if !should_send && response_event.usage.is_some() {
                                    // The duplicate full text is suppressed, but the usage
                                    // riding on response.completed must still reach the
                                    // client — as a bare usage chunk (OpenAI include_usage
                                    // shape), or the cost meter stays blind.
                                    let usage_only = ResponseEvent {
                                        choices: vec![],
                                        ..response_event.clone()
                                    };
                                    if tx.send(Ok(usage_only)).await.is_err() {
                                        return;
                                    }
                                }
                                if should_send {
                                    // Update last sent content if this is a non-empty message
                                    if let Some(ref new_content) = content {
                                        if !new_content.is_empty() {
                                            last_sent_content = Some(new_content.clone());
                                        }
                                    }
                                    if tx.send(Ok(response_event)).await.is_err() {
                                        // Channel closed, stop processing
                                        return;
                                    }
                                }
                            }
                        }
                        Err(e) => {
                            println!("⚠️  JSON parse error in upstream SSE event: {}", e);
                            // Send a structured error response for malformed JSON
                            let error_event = ResponseEvent {
                                id: format!("error-{}", uuid::Uuid::new_v4()),
                                object: "chat.completion.chunk".to_string(),
                                created: std::time::SystemTime::now()
                                    .duration_since(std::time::UNIX_EPOCH)
                                    .unwrap_or_default()
                                    .as_secs() as i64,
                                model: request.model.clone(),
                        usage: None,
                                choices: vec![ResponseChoice {
                                    index: 0,
                                    delta: ResponseDelta {
                                        role: Some("assistant".to_string()),
                                        content: Some(format!("JSON parse error: {}", e)),
                                        tool_calls: None,
                                    },
                                    finish_reason: Some("error".to_string()),
                                }],
                            };
                            let _ = tx.send(Ok(error_event)).await;
                            continue;
                        }
                    }
                }
            }
        }
    });

    Ok(rx)
}

fn parse_sse_event(event: &Value, tool_call_index: usize) -> Option<ResponseEvent> {
    let event_type = event.get("type").and_then(Value::as_str);
    if event_type.is_some_and(|event_type| event_type.starts_with("response.web_search_call.")) {
        return None;
    }

    // Hosted search is completed by OpenAI. It is observability, not a client
    // function call, so never translate it into a tool_call for Praxis.
    if matches!(
        event_type,
        Some("response.output_item.added" | "response.output_item.done")
    ) && event
        .get("item")
        .and_then(|item| item.get("type"))
        .and_then(Value::as_str)
        == Some("web_search_call")
    {
        return None;
    }

    // Streaming Responses API: a finished output item. A function_call item must be surfaced as an
    // OpenAI tool_calls delta — the backend streams tool calls here (response.output_item.done). The
    // old code only scanned response.completed's output[] (function_call may not even be there), so
    // tool calls never reached the client — the model would narrate the call but never make it.
    if event_type == Some("response.output_item.done") {
        if let Some(item) = event.get("item") {
            if item.get("type").and_then(|t| t.as_str()) == Some("function_call") {
                let name = item
                    .get("name")
                    .and_then(|n| n.as_str())
                    .unwrap_or("")
                    .to_string();
                let arguments = item
                    .get("arguments")
                    .and_then(|a| a.as_str())
                    .unwrap_or("")
                    .to_string();
                let call_id = item
                    .get("call_id")
                    .and_then(|id| id.as_str())
                    .map(|s| s.to_string())
                    .unwrap_or_else(|| format!("call_{}", uuid::Uuid::new_v4()));
                return Some(ResponseEvent {
                    id: format!("chatcmpl-{}", &uuid::Uuid::new_v4().to_string()[..8]),
                    object: "chat.completion.chunk".to_string(),
                    created: std::time::SystemTime::now()
                        .duration_since(std::time::UNIX_EPOCH)
                        .unwrap_or_default()
                        .as_secs() as i64,
                    model: "gpt-4".to_string(),
                    usage: None,
                    choices: vec![ResponseChoice {
                        index: 0,
                        delta: ResponseDelta {
                            role: Some("assistant".to_string()),
                            content: None,
                            // parallel_tool_calls: every function_call item of one
                            // response gets its own slot; a repeated index would make
                            // the client concatenate argument JSON of distinct calls.
                            tool_calls: Some(serde_json::json!([{
                                "id": call_id,
                                "type": "function",
                                "index": tool_call_index,
                                "function": { "name": name, "arguments": arguments }
                            }])),
                        },
                        finish_reason: Some("tool_calls".to_string()),
                    }],
                });
            }
        }
        // A non-function item (e.g. the assistant message) carries its text in response.completed.
        return None;
    }

    // Try to extract content from various possible structures
    let content = extract_content_from_chatgpt_response(event);
    let model = event
        .get("model")
        .or_else(|| {
            event
                .get("response")
                .and_then(|response| response.get("model"))
        })
        .and_then(|v| v.as_str())
        .unwrap_or("gpt-4")
        .to_string();

    // Create OpenAI-compatible response
    Some(ResponseEvent {
        usage: extract_completed_usage(event),
        id: event
            .get("id")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string())
            .unwrap_or_else(|| format!("chatcmpl-{}", &uuid::Uuid::new_v4().to_string()[..8])),
        object: "chat.completion.chunk".to_string(),
        created: event
            .get("created")
            .and_then(|v| v.as_i64())
            .unwrap_or_else(|| {
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap_or_default()
                    .as_secs() as i64
            }),
        model,
        choices: if let Some(content) = content {
            vec![ResponseChoice {
                index: 0,
                delta: ResponseDelta {
                    role: Some("assistant".to_string()),
                    content: Some(content),
                    tool_calls: None,
                },
                finish_reason: event
                    .get("finish_reason")
                    .and_then(|v| v.as_str())
                    .map(|s| s.to_string()),
            }]
        } else {
            // Check if this is a finish event
            if event.get("finish_reason").is_some() {
                vec![ResponseChoice {
                    index: 0,
                    delta: ResponseDelta {
                        role: None,
                        content: None,
                        tool_calls: None,
                    },
                    finish_reason: event
                        .get("finish_reason")
                        .and_then(|v| v.as_str())
                        .map(|s| s.to_string()),
                }]
            } else {
                vec![]
            }
        },
    })
}

/// Token accounting rides on response.completed (usage.input_tokens/output_tokens,
/// reasoning included in output).  Forward it so the client's cost meter can see.
fn extract_completed_usage(event: &Value) -> Option<crate::core::models::Usage> {
    if safe_event_type(event) != "response.completed" {
        return None;
    }
    let usage = event.get("response")?.get("usage")?;
    let prompt = usage.get("input_tokens").and_then(Value::as_u64).unwrap_or(0) as u32;
    let completion = usage.get("output_tokens").and_then(Value::as_u64).unwrap_or(0) as u32;
    if prompt == 0 && completion == 0 {
        return None;
    }
    let total = usage
        .get("total_tokens")
        .and_then(Value::as_u64)
        .unwrap_or(u64::from(prompt + completion)) as u32;
    // 02.08.2026: деталь кэша. Апстрим (Responses API) кладёт её в
    // `usage.input_tokens_details.cached_tokens`; OpenAI-совместимая форма, которую ждёт
    // клиент, называет то же поле `prompt_tokens_details.cached_tokens`. Читаем оба имени:
    // одно — то, что приходит сегодня, второе — то, что придёт, если апстрим перейдёт на
    // chat-совместимую форму. Отсутствие поля остаётся ОТСУТСТВИЕМ (None), а не нулём:
    // «кэш не сработал» и «провайдер не сказал» — разные факты, и путать их дороже.
    let cached = usage
        .get("input_tokens_details")
        .or_else(|| usage.get("prompt_tokens_details"))
        .and_then(|details| details.get("cached_tokens"))
        .and_then(Value::as_u64)
        .map(|value| crate::core::models::PromptTokensDetails {
            cached_tokens: value as u32,
        });
    Some(crate::core::models::Usage {
        prompt_tokens: prompt,
        completion_tokens: completion,
        total_tokens: total,
        prompt_tokens_details: cached,
    })
}

fn safe_event_type(event: &Value) -> &str {
    event
        .get("type")
        .and_then(Value::as_str)
        .filter(|value| {
            !value.is_empty()
                && value.len() <= 96
                && value
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
        })
        .unwrap_or("unknown")
}

fn extract_content_from_chatgpt_response(event: &Value) -> Option<String> {
    // Try multiple possible paths for content in ChatGPT's response format
    if let Some(response) = event.get("response") {
        if let Some(content) = extract_responses_message(response) {
            return Some(content);
        }
    }

    // Handle tool calls specifically
    if let Some(response) = event.get("response") {
        if let Some(output) = response.get("output").and_then(|o| o.as_array()) {
            // Look for function_call items in the output
            for item in output {
                if let Some(item_obj) = item.as_object() {
                    if let Some(item_type) = item_obj.get("type").and_then(|t| t.as_str()) {
                        // Tool calls are surfaced from response.output_item.done above — skip them
                        // here (don't bail out, or a function_call before the message would swallow
                        // the assistant's text).
                        if item_type == "function_call" {
                            continue;
                        }
                        // Handle message type items that might contain tool results
                        else if item_type == "message" {
                            if let Some(content) = item_obj.get("content").and_then(|c| {
                                c.as_array().and_then(|arr| {
                                    arr.first().and_then(|first| {
                                        first.get("text").and_then(|t| t.as_str())
                                    })
                                })
                            }) {
                                return Some(content.to_string());
                            }
                        }
                    }
                }
            }
        }
    }

    // Standard OpenAI format
    if let Some(choices) = event.get("choices").and_then(|c| c.as_array()) {
        if let Some(choice) = choices.first() {
            if let Some(delta) = choice.get("delta") {
                if let Some(content) = delta.get("content").and_then(|c| c.as_str()) {
                    return Some(content.to_string());
                }
            }
            if let Some(message) = choice.get("message") {
                if let Some(content) = message.get("content").and_then(|c| c.as_str()) {
                    return Some(content.to_string());
                }
            }
        }
    }

    // ChatGPT Responses API format - direct content field
    if let Some(content) = event.get("content").and_then(|c| c.as_str()) {
        return Some(content.to_string());
    }

    // ChatGPT Responses API format - message field
    if let Some(message) = event.get("message") {
        if let Some(content) = message.get("content").and_then(|c| c.as_str()) {
            return Some(content.to_string());
        }
        if let Some(content) = message.as_str() {
            return Some(content.to_string());
        }
    }

    // ChatGPT Responses API format - response field
    if let Some(response) = event.get("response") {
        if let Some(content) = response.get("content").and_then(|c| c.as_str()) {
            return Some(content.to_string());
        }
        if let Some(content) = response.as_str() {
            return Some(content.to_string());
        }
    }

    // ChatGPT Responses API format - text field
    if let Some(text) = event.get("text").and_then(|t| t.as_str()) {
        return Some(text.to_string());
    }

    // ChatGPT Responses API format - delta field
    if let Some(delta) = event.get("delta") {
        if let Some(content) = delta.get("content").and_then(|c| c.as_str()) {
            return Some(content.to_string());
        }
        if let Some(text) = delta.get("text").and_then(|t| t.as_str()) {
            return Some(text.to_string());
        }
    }

    None
}

fn extract_responses_message(response: &Value) -> Option<String> {
    let output = response.get("output").and_then(Value::as_array)?;
    for item in output {
        if item.get("type").and_then(Value::as_str) != Some("message") {
            continue;
        }

        let parts = item.get("content").and_then(Value::as_array)?;
        let mut text_parts = Vec::new();
        let mut citations = Vec::new();
        let mut seen_urls = HashSet::new();

        for part in parts {
            if let Some(text) = part.get("text").and_then(Value::as_str) {
                text_parts.push(text);
            }
            let Some(annotations) = part.get("annotations").and_then(Value::as_array) else {
                continue;
            };
            for annotation in annotations {
                let Some((url, title)) = visible_url_citation(annotation) else {
                    continue;
                };
                if citations.len() < MAX_VISIBLE_CITATIONS && seen_urls.insert(url.clone()) {
                    citations.push((url, title));
                }
            }
        }

        if text_parts.is_empty() {
            continue;
        }
        let mut text = text_parts.join("\n");
        if !citations.is_empty() {
            text.push_str("\n\nИсточники:");
            for (url, title) in citations {
                text.push_str("\n- ");
                if let Some(title) = title {
                    text.push_str(&title);
                    text.push_str(" — ");
                }
                text.push_str(&url);
            }
        }
        return Some(text);
    }

    None
}

fn visible_url_citation(annotation: &Value) -> Option<(String, Option<String>)> {
    let citation = if annotation.get("type").and_then(Value::as_str) == Some("url_citation") {
        annotation.get("url_citation").unwrap_or(annotation)
    } else {
        annotation.get("url_citation")?
    };
    let raw_url = citation.get("url").and_then(Value::as_str)?;
    if raw_url.len() > 8 * 1024 {
        return None;
    }
    let parsed = Url::parse(raw_url).ok()?;
    if !matches!(parsed.scheme(), "http" | "https")
        || parsed.host_str().is_none()
        || !parsed.username().is_empty()
        || parsed.password().is_some()
    {
        return None;
    }

    let title = citation
        .get("title")
        .and_then(Value::as_str)
        .map(|title| title.split_whitespace().collect::<Vec<_>>().join(" "))
        .filter(|title| !title.is_empty())
        .map(|title| title.chars().take(160).collect());
    Some((parsed.to_string(), title))
}

fn upstream_error_message(status: reqwest::StatusCode, body: &str) -> String {
    let parsed = serde_json::from_str::<Value>(body).ok();
    let error = parsed
        .as_ref()
        .and_then(|value| value.get("error").or(Some(value)));

    let code = error
        .and_then(|value| value.get("code").or_else(|| value.get("type")))
        .and_then(Value::as_str)
        .filter(|value| {
            !value.is_empty()
                && value.len() <= 64
                && value
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
        });
    let message = error
        .and_then(|value| value.get("message"))
        .and_then(Value::as_str)
        .and_then(bounded_error_text);

    match (code, message) {
        (Some(code), Some(message)) => {
            format!("Upstream error {status} ({code}): {message}")
        }
        (Some(code), None) => format!("Upstream error {status} ({code})"),
        (None, Some(message)) => format!("Upstream error {status}: {message}"),
        (None, None) => format!("Upstream error {status}"),
    }
}

fn subscription_quota_error(status: reqwest::StatusCode, body: &str) -> bool {
    if status != reqwest::StatusCode::TOO_MANY_REQUESTS {
        return false;
    }
    let parsed = serde_json::from_str::<Value>(body).ok();
    let error = parsed
        .as_ref()
        .and_then(|value| value.get("error").or(Some(value)));
    let code = error
        .and_then(|value| value.get("code").or_else(|| value.get("type")))
        .and_then(Value::as_str);
    code == Some("usage_limit_reached") || body.contains("usage_limit_reached")
}

fn bounded_error_text(raw: &str) -> Option<String> {
    let normalized = raw.split_whitespace().collect::<Vec<_>>().join(" ");
    if normalized.is_empty() {
        return None;
    }

    let mut chars = normalized.chars();
    let mut bounded: String = chars.by_ref().take(MAX_UPSTREAM_ERROR_CHARS).collect();
    if chars.next().is_some() {
        bounded.push('…');
    }
    Some(bounded)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn only_explicit_subscription_exhaustion_triggers_failover() {
        assert!(subscription_quota_error(
            reqwest::StatusCode::TOO_MANY_REQUESTS,
            r#"{"error":{"code":"usage_limit_reached"}}"#,
        ));
        assert!(!subscription_quota_error(
            reqwest::StatusCode::TOO_MANY_REQUESTS,
            r#"{"error":{"code":"rate_limit_exceeded"}}"#,
        ));
        assert!(!subscription_quota_error(
            reqwest::StatusCode::UNAUTHORIZED,
            r#"{"error":{"code":"usage_limit_reached"}}"#,
        ));
    }

    #[test]
    fn carries_cyrillic_split_across_chunk_boundary() {
        // "привет" — every letter is two bytes; cut the stream inside the "и".
        let text = "привет";
        let bytes = text.as_bytes();
        let cut = 3; // middle of the second character
        let mut carry = Vec::new();
        let mut out = String::new();
        out.push_str(&decode_stream_chunk(&bytes[..cut], &mut carry));
        assert_eq!(carry.len(), 1, "half a character must be held back, not emitted");
        out.push_str(&decode_stream_chunk(&bytes[cut..], &mut carry));
        assert_eq!(out, text);
        assert!(carry.is_empty());
        assert!(!out.contains('\u{FFFD}'));
    }

    #[test]
    fn carries_across_byte_at_a_time_delivery() {
        let text = "приятно, что я это ты";
        let mut carry = Vec::new();
        let mut out = String::new();
        for b in text.as_bytes() {
            out.push_str(&decode_stream_chunk(&[*b], &mut carry));
        }
        assert_eq!(out, text);
        assert!(carry.is_empty());
    }

    #[test]
    fn four_byte_sequences_survive_every_split() {
        let text = "🌍🌏"; // 4 bytes each — exercises the longest carry
        let bytes = text.as_bytes();
        for cut in 1..bytes.len() {
            let mut carry = Vec::new();
            let mut out = decode_stream_chunk(&bytes[..cut], &mut carry);
            out.push_str(&decode_stream_chunk(&bytes[cut..], &mut carry));
            assert_eq!(out, text, "split at byte {} lost data", cut);
            assert!(carry.is_empty(), "split at byte {} left a carry", cut);
        }
    }

    #[test]
    fn genuinely_invalid_bytes_are_replaced_not_carried() {
        let mut carry = Vec::new();
        let mut input = b"ok".to_vec();
        input.push(0xFF); // never valid in UTF-8
        input.extend_from_slice("да".as_bytes());
        let out = decode_stream_chunk(&input, &mut carry);
        assert_eq!(out, "ok\u{FFFD}да");
        assert!(carry.is_empty(), "broken bytes must not stall the stream");
    }

    #[test]
    fn ascii_only_stream_is_untouched() {
        let mut carry = Vec::new();
        let out = decode_stream_chunk(b"data: {\"a\":1}\n\n", &mut carry);
        assert_eq!(out, "data: {\"a\":1}\n\n");
        assert!(carry.is_empty());
    }

    #[test]
    fn translates_text_and_image_url_parts_to_responses_content() {
        let request: ChatRequest = serde_json::from_value(json!({
            "model": "gpt-5.5",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "What is this?"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "https://example.com/photo.jpg",
                            "detail": "high"
                        }
                    }
                ]
            }]
        }))
        .unwrap();
        request.validate_content().unwrap();

        assert_eq!(
            build_responses_input(&request.messages),
            vec![json!({
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "What is this?"},
                    {
                        "type": "input_image",
                        "image_url": "https://example.com/photo.jpg",
                        "detail": "high"
                    }
                ]
            })]
        );
    }

    #[test]
    fn preserves_legacy_string_null_and_tool_loop_mapping() {
        let request: ChatRequest = serde_json::from_value(json!({
            "model": "gpt-5.5",
            "messages": [
                {"role": "system", "content": "rules"},
                {"role": "user", "content": "hello"},
                {
                    "role": "assistant",
                    "content": null,
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": "{\"q\":1}"}
                    }]
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "content": "done"
                }
            ]
        }))
        .unwrap();
        request.validate_content().unwrap();

        assert_eq!(
            build_responses_input(&request.messages),
            vec![
                json!({"role": "user", "content": "<system>\nrules\n</system>"}),
                json!({"role": "user", "content": "hello"}),
                json!({
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "lookup",
                    "arguments": "{\"q\":1}"
                }),
                json!({
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": "done"
                })
            ]
        );
    }

    #[test]
    fn omits_image_detail_when_client_omits_it() {
        let request: ChatRequest = serde_json::from_value(json!({
            "model": "gpt-5.5",
            "messages": [{
                "role": "user",
                "content": [{
                    "type": "image_url",
                    "image_url": {"url": "https://example.com/photo.jpg"}
                }]
            }]
        }))
        .unwrap();

        let input = build_responses_input(&request.messages);
        assert!(input[0]["content"][0].get("detail").is_none());
    }

    #[test]
    fn maps_hosted_search_without_unsupported_max_tool_calls() {
        let request: ChatRequest = serde_json::from_value(json!({
            "model": "gpt-5.6-sol",
            "messages": [{"role": "user", "content": "latest release"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "web_read",
                        "description": "Read a URL",
                        "parameters": {"type": "object"},
                        "strict": false
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

        let payload = build_responses_payload(
            &request,
            "instructions".to_string(),
            build_responses_input(&request.messages),
            Some("none"),
            Some("cache-affinity-1"),
            false,
        );
        assert_eq!(payload["model"], "gpt-5.6-sol");
        assert_eq!(payload["reasoning"]["effort"], "none");
        assert_eq!(payload["prompt_cache_key"], "cache-affinity-1");
        assert!(payload.get("max_tool_calls").is_none());
        assert_eq!(payload["tool_choice"], "auto");
        assert_eq!(payload["parallel_tool_calls"], false);
        assert_eq!(payload["tools"][0]["type"], "function");
        assert_eq!(payload["tools"][0]["strict"], false);
        assert_eq!(payload["tools"][1]["type"], "web_search");
        assert_eq!(payload["tools"][1]["external_web_access"], true);
        assert_eq!(payload["tools"][1]["search_context_size"], "medium");
        assert!(payload["tools"][1].get("max_uses").is_none());
    }

    #[test]
    fn reasoning_knob_is_validated_and_optional() {
        let request: ChatRequest = serde_json::from_value(json!({
            "model": "gpt-5.6-sol",
            "messages": [{"role": "user", "content": "hi"}]
        }))
        .unwrap();

        let omitted = build_responses_payload(
            &request,
            "instructions".to_string(),
            build_responses_input(&request.messages),
            None,
            None,
            true,
        );
        assert!(omitted.get("reasoning").is_none());
        assert!(omitted.get("prompt_cache_key").is_none());

        let junk = build_responses_payload(
            &request,
            "instructions".to_string(),
            build_responses_input(&request.messages),
            Some("galactic"),
            None,
            true,
        );
        assert!(junk.get("reasoning").is_none());

        let low = build_responses_payload(
            &request,
            "instructions".to_string(),
            build_responses_input(&request.messages),
            Some("low"),
            None,
            true,
        );
        assert_eq!(low["reasoning"]["effort"], "low");
    }

    #[test]
    fn conversation_affinity_is_stable_within_a_turn_and_uuid_shaped() {
        let request: ChatRequest = serde_json::from_value(json!({
            "model": "gpt-5.6-sol",
            "messages": [
                {"role": "system", "content": "persona prompt"},
                {"role": "user", "content": "first"}
            ]
        }))
        .unwrap();
        let mut longer: ChatRequest = serde_json::from_value(json!({
            "model": "gpt-5.6-sol",
            "messages": [
                {"role": "system", "content": "persona prompt"},
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "tool step"},
                {"role": "user", "content": "tool result"}
            ]
        }))
        .unwrap();
        // Same first message => same affinity across tool-loop iterations.
        assert_eq!(conversation_affinity(&request), conversation_affinity(&longer));
        let id = conversation_affinity(&request);
        assert_eq!(id.len(), 36);
        assert_eq!(id.matches('-').count(), 4);
        // Different conversation (different system prompt) => different affinity.
        longer.messages[0].content =
            Some(MessageContent::Text("another persona".to_string()));
        assert_ne!(conversation_affinity(&request), conversation_affinity(&longer));
    }

    #[test]
    fn completed_usage_is_forwarded_and_other_events_carry_none() {
        let event = json!({
            "type": "response.completed",
            "response": {
                "model": "gpt-5.6-sol",
                "usage": {"input_tokens": 1200, "output_tokens": 340, "total_tokens": 1540}
            }
        });
        let usage = extract_completed_usage(&event).expect("usage forwarded");
        assert_eq!(usage.prompt_tokens, 1200);
        assert_eq!(usage.completion_tokens, 340);
        assert_eq!(usage.total_tokens, 1540);
        let chunk = parse_sse_event(&event, 0).expect("completed event parsed");
        assert_eq!(chunk.usage.as_ref().map(|u| u.completion_tokens), Some(340));
        assert!(extract_completed_usage(&json!({"type": "response.output_text.delta"})).is_none());
        assert!(extract_completed_usage(
            &json!({"type": "response.completed", "response": {"usage": {}}})).is_none());
        // Без детали кэша поле остаётся ОТСУТСТВУЮЩИМ, а не нулевым.
        assert!(usage.prompt_tokens_details.is_none());
    }

    #[test]
    fn cached_prefix_accounting_survives_the_relay() {
        // 02.08.2026: реле схлопывало usage до трёх чисел, и деталь кэша терялась —
        // у Praxis в учёте стояли нули по всем ходам через gpt, что читалось как
        // «кэш не работает», хотя означало «не сообщено».
        let upstream = json!({
            "type": "response.completed",
            "response": {"usage": {
                "input_tokens": 42000, "output_tokens": 300, "total_tokens": 42300,
                "input_tokens_details": {"cached_tokens": 18800}
            }}
        });
        let usage = extract_completed_usage(&upstream).expect("usage forwarded");
        assert_eq!(
            usage.prompt_tokens_details.as_ref().map(|d| d.cached_tokens),
            Some(18800)
        );
        // Chat-совместимое имя того же поля читается тоже.
        let chat_shaped = json!({
            "type": "response.completed",
            "response": {"usage": {
                "input_tokens": 10, "output_tokens": 1, "total_tokens": 11,
                "prompt_tokens_details": {"cached_tokens": 7}
            }}
        });
        assert_eq!(
            extract_completed_usage(&chat_shaped)
                .and_then(|u| u.prompt_tokens_details)
                .map(|d| d.cached_tokens),
            Some(7)
        );
        // И доезжает до клиента в SSE-чанке, а не только внутри реле.
        let chunk = parse_sse_event(&upstream, 0).expect("completed event parsed");
        assert_eq!(
            chunk.usage.and_then(|u| u.prompt_tokens_details).map(|d| d.cached_tokens),
            Some(18800)
        );
    }

    #[test]
    fn parallel_function_calls_get_distinct_indexes() {
        let mk = |call: &str| json!({
            "type": "response.output_item.done",
            "item": {"type": "function_call", "name": "t", "arguments": "{}", "call_id": call}
        });
        let first = parse_sse_event(&mk("call-a"), 0).unwrap();
        let second = parse_sse_event(&mk("call-b"), 1).unwrap();
        let idx = |ev: &ResponseEvent| {
            ev.choices[0].delta.tool_calls.as_ref().unwrap()[0]["index"].as_u64()
        };
        assert_eq!(idx(&first), Some(0));
        assert_eq!(idx(&second), Some(1));
        assert_eq!(
            second.choices[0].delta.tool_calls.as_ref().unwrap()[0]["id"],
            "call-b"
        );
    }

    #[test]
    fn payload_carries_parallel_flag_and_minimal_stub_is_small() {
        let request: ChatRequest = serde_json::from_value(json!({
            "model": "gpt-5.6-sol",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {
                "name": "t", "description": "", "parameters": {"type": "object"}, "strict": false
            }}]
        }))
        .unwrap();
        let parallel = build_responses_payload(
            &request, "i".to_string(), build_responses_input(&request.messages),
            None, None, true,
        );
        assert_eq!(parallel["parallel_tool_calls"], true);
        let serial = build_responses_payload(
            &request, "i".to_string(), build_responses_input(&request.messages),
            None, None, false,
        );
        assert_eq!(serial["parallel_tool_calls"], false);
        assert!(MINIMAL_INSTRUCTIONS.len() < 600);
    }

    #[test]
    fn codex_request_has_version_gated_user_agent() {
        let request = build_codex_request(
            &Client::new(),
            "test-access-token",
            "test-account",
            "test-session",
            &json!({"model": "gpt-5.6-luna"}),
        )
        .build()
        .unwrap();

        assert_eq!(CODEX_USER_AGENT, "codex_cli_rs/0.144.0");
        assert_eq!(CODEX_CLI_VERSION, "0.144.0");
        assert_eq!(request.url().as_str(), CHATGPT_CODEX_RESPONSES_URL);
        assert_eq!(
            request.headers().get(reqwest::header::USER_AGENT).unwrap(),
            CODEX_USER_AGENT
        );
        assert_eq!(
            request.headers().get("originator").unwrap(),
            CODEX_ORIGINATOR
        );
    }

    #[test]
    fn hosted_search_events_never_become_client_tool_calls() {
        for event in [
            json!({"type": "response.web_search_call.in_progress"}),
            json!({
                "type": "response.output_item.added",
                "item": {"type": "web_search_call", "id": "ws_1"}
            }),
            json!({
                "type": "response.output_item.done",
                "item": {"type": "web_search_call", "id": "ws_1"}
            }),
        ] {
            assert!(parse_sse_event(&event, 0).is_none());
        }
    }

    #[test]
    fn completed_search_response_emits_citations_and_actual_model() {
        let event = json!({
            "type": "response.completed",
            "response": {
                "model": "gpt-5.6-terra",
                "output": [
                    {"type": "web_search_call", "id": "ws_1", "status": "completed"},
                    {
                        "type": "message",
                        "content": [{
                            "type": "output_text",
                            "text": "Current answer",
                            "annotations": [{
                                "type": "url_citation",
                                "url": "https://example.com/source",
                                "title": "Primary source"
                            }]
                        }]
                    }
                ]
            }
        });

        let translated = parse_sse_event(&event, 0).unwrap();
        assert_eq!(translated.model, "gpt-5.6-terra");
        let delta = &translated.choices[0].delta;
        assert_eq!(delta.tool_calls, None);
        assert_eq!(
            delta.content.as_deref(),
            Some("Current answer\n\nИсточники:\n- Primary source — https://example.com/source")
        );
    }

    #[test]
    fn response_citations_are_visible_deduplicated_and_capped() {
        let mut annotations = vec![json!({
            "type": "url_citation",
            "url": "ftp://unsafe.example/ignored",
            "title": "unsafe"
        })];
        for index in 0..14 {
            let citation = json!({
                "url": format!("https://source.example/{index}"),
                "title": if index == 0 { "Source\n 0" } else { "Source" }
            });
            if index == 1 {
                annotations.push(json!({
                    "type": "url_citation",
                    "url_citation": citation
                }));
                annotations.push(json!({
                    "type": "url_citation",
                    "url": "https://source.example/1",
                    "title": "duplicate"
                }));
            } else {
                annotations.push(json!({
                    "type": "url_citation",
                    "url": citation["url"],
                    "title": citation["title"]
                }));
            }
        }
        let response = json!({
            "output": [{
                "type": "message",
                "content": [{
                    "type": "output_text",
                    "text": "Answer already mentions https://source.example/0",
                    "annotations": annotations
                }]
            }]
        });

        let text = extract_responses_message(&response).unwrap();
        let source_lines: Vec<_> = text.lines().filter(|line| line.starts_with("- ")).collect();
        assert_eq!(source_lines.len(), MAX_VISIBLE_CITATIONS);
        assert_eq!(
            source_lines
                .iter()
                .filter(|line| line.ends_with("source.example/1"))
                .count(),
            1
        );
        assert!(text.contains("Source 0 — https://source.example/0"));
        assert!(text.contains("https://source.example/11"));
        assert!(!text.contains("https://source.example/12"));
        assert!(!text.contains("ftp://unsafe.example"));
    }

    #[test]
    fn logs_only_bounded_event_labels_and_client_safe_upstream_errors() {
        assert_eq!(
            safe_event_type(&json!({"type": "response.output_text.delta"})),
            "response.output_text.delta"
        );
        assert_eq!(safe_event_type(&json!({"type": "bad\nprivate"})), "unknown");

        let long_message = format!("invalid\nrequest {}", "x".repeat(700));
        let body = json!({
            "error": {
                "code": "invalid_request_error",
                "message": long_message,
                "private_request_echo": "must-not-leak"
            },
            "private": "must-not-leak-either"
        })
        .to_string();
        let message = upstream_error_message(reqwest::StatusCode::BAD_REQUEST, &body);
        assert!(message.contains("invalid_request_error"));
        assert!(!message.contains('\n'));
        assert!(!message.contains("must-not-leak"));
        assert!(message.chars().count() <= MAX_UPSTREAM_ERROR_CHARS + 80);

        assert_eq!(
            upstream_error_message(reqwest::StatusCode::BAD_GATEWAY, "raw private response"),
            "Upstream error 502 Bad Gateway"
        );
    }
}
