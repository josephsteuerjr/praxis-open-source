//! Read the authenticated Codex allowance without spending a model turn.
//!
//! OpenAI's Codex client reads the ChatGPT `wham/usage` resource and represents
//! subscription windows with the `x-codex-*-used-percent/window-minutes/reset-at`
//! header family.  The relay exposes the same facts as a small, allow-listed JSON
//! document and mirrors the primary/secondary values in those response headers.

use anyhow::{anyhow, bail, Context, Result};
use axum::http::{HeaderMap, HeaderName, HeaderValue};
use chrono::Utc;
use serde_json::{json, Value};
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::Mutex;

use crate::core::config::Config;
use crate::login::lib::{AuthMode, CodexAuth};

const CHATGPT_USAGE_URL: &str = "https://chatgpt.com/backend-api/wham/usage";
const CODEX_USER_AGENT: &str = "codex_cli_rs/0.144.0";
const CACHE_TTL: Duration = Duration::from_secs(60);
const MAX_USAGE_BODY_BYTES: usize = 1024 * 1024;

#[derive(Clone, Default)]
pub struct LimitsCache {
    inner: Arc<Mutex<Option<CachedLimits>>>,
}

struct CachedLimits {
    fetched: Instant,
    value: Value,
}

impl LimitsCache {
    pub async fn get(&self, config: &Config) -> Result<Value> {
        // Holding this small async mutex through the refresh also prevents a
        // status-page burst from fanning out into several upstream requests.
        let mut guard = self.inner.lock().await;
        if let Some(cached) = guard.as_ref() {
            if cached.fetched.elapsed() < CACHE_TTL {
                return Ok(cached.value.clone());
            }
        }

        let value = fetch_usage(config).await?;
        *guard = Some(CachedLimits {
            fetched: Instant::now(),
            value: value.clone(),
        });
        Ok(value)
    }
}

async fn fetch_usage(config: &Config) -> Result<Value> {
    let auth = CodexAuth::from_codex_home(&config.codex_home)
        .context("failed to load Codex authentication")?
        .ok_or_else(|| anyhow!("Codex authentication is not configured"))?;
    if auth.mode != AuthMode::ChatGPT {
        bail!("ChatGPT authentication is required for subscription limits");
    }
    let tokens = auth
        .get_token_data()
        .await
        .context("failed to refresh Codex authentication")?;
    if tokens.access_token.is_empty() {
        bail!("Codex access token is empty");
    }
    let account_id = tokens
        .account_id
        .filter(|value| !value.trim().is_empty())
        .ok_or_else(|| anyhow!("ChatGPT account id is missing"))?;

    let response = reqwest::Client::new()
        .get(CHATGPT_USAGE_URL)
        .bearer_auth(tokens.access_token)
        .header("ChatGPT-Account-ID", account_id)
        .header(reqwest::header::USER_AGENT, CODEX_USER_AGENT)
        .header(reqwest::header::ACCEPT, "application/json")
        .send()
        .await
        .context("OpenAI usage request failed")?;
    let status = response.status();
    if !status.is_success() {
        // The upstream body can contain account details.  Never reflect it.
        bail!("OpenAI usage endpoint returned {status}");
    }
    if response
        .content_length()
        .is_some_and(|length| length > MAX_USAGE_BODY_BYTES as u64)
    {
        bail!("OpenAI usage response is unexpectedly large");
    }
    let body = response
        .bytes()
        .await
        .context("failed to read OpenAI usage response")?;
    if body.len() > MAX_USAGE_BODY_BYTES {
        bail!("OpenAI usage response is unexpectedly large");
    }
    let raw: Value = serde_json::from_slice(&body).context("invalid OpenAI usage JSON")?;
    normalize_usage(&raw)
}

fn normalized_window(value: Option<&Value>) -> Value {
    let value = value.and_then(Value::as_object);
    let used = value
        .and_then(|item| item.get("used_percent"))
        .and_then(Value::as_f64)
        .filter(|number| number.is_finite());
    json!({
        "used_percent": used,
        "remaining_percent": used.map(|number| (100.0 - number).clamp(0.0, 100.0)),
        "limit_window_seconds": value.and_then(|item| item.get("limit_window_seconds")).and_then(Value::as_i64),
        "reset_after_seconds": value.and_then(|item| item.get("reset_after_seconds")).and_then(Value::as_i64),
        "reset_at": value.and_then(|item| item.get("reset_at")).and_then(Value::as_i64),
    })
}

fn normalized_rate_limit(value: Option<&Value>) -> Value {
    let value = value.and_then(Value::as_object);
    json!({
        "allowed": value.and_then(|item| item.get("allowed")).and_then(Value::as_bool),
        "limit_reached": value.and_then(|item| item.get("limit_reached")).and_then(Value::as_bool),
        "primary_window": normalized_window(value.and_then(|item| item.get("primary_window"))),
        "secondary_window": normalized_window(value.and_then(|item| item.get("secondary_window"))),
    })
}

fn bounded_number_array(value: Option<&Value>) -> Value {
    let values = value
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .take(8)
                .filter(|item| item.is_number())
                .cloned()
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    Value::Array(values)
}

fn normalized_credits(value: Option<&Value>) -> Value {
    let value = value.and_then(Value::as_object);
    json!({
        "unlimited": value.and_then(|item| item.get("unlimited")).and_then(Value::as_bool),
        "balance": value.and_then(|item| item.get("balance")).and_then(|item| {
            if item.is_string() || item.is_number() { Some(item.clone()) } else { None }
        }),
        "approx_local_messages": bounded_number_array(value.and_then(|item| item.get("approx_local_messages"))),
        "approx_cloud_messages": bounded_number_array(value.and_then(|item| item.get("approx_cloud_messages"))),
    })
}

fn normalize_usage(raw: &Value) -> Result<Value> {
    let root = raw
        .as_object()
        .ok_or_else(|| anyhow!("OpenAI usage payload is not an object"))?;
    let mut additional = Vec::new();
    if let Some(items) = root.get("additional_rate_limits").and_then(Value::as_array) {
        for item in items.iter().take(16).filter_map(Value::as_object) {
            additional.push(json!({
                "metered_feature": item.get("metered_feature").and_then(Value::as_str),
                "limit_name": item.get("limit_name").and_then(Value::as_str),
                "rate_limit": normalized_rate_limit(item.get("rate_limit")),
            }));
        }
    }

    Ok(json!({
        "schema": "relay.openai.limits.v1",
        "source": "openai_codex_usage",
        "fetched_at": Utc::now().to_rfc3339(),
        "plan_type": root.get("plan_type").and_then(Value::as_str),
        "rate_limit": normalized_rate_limit(root.get("rate_limit")),
        "credits": normalized_credits(root.get("credits")),
        "additional_rate_limits": additional,
    }))
}

fn insert(headers: &mut HeaderMap, name: &'static str, value: impl ToString) {
    if let Ok(value) = HeaderValue::from_str(&value.to_string()) {
        headers.insert(HeaderName::from_static(name), value);
    }
}

fn mirror_window(headers: &mut HeaderMap, prefix: &'static str, value: Option<&Value>) {
    let Some(value) = value.and_then(Value::as_object) else {
        return;
    };
    if let Some(used) = value.get("used_percent").and_then(Value::as_f64) {
        insert(
            headers,
            match prefix {
                "primary" => "x-codex-primary-used-percent",
                _ => "x-codex-secondary-used-percent",
            },
            used,
        );
    }
    if let Some(seconds) = value.get("limit_window_seconds").and_then(Value::as_i64) {
        insert(
            headers,
            match prefix {
                "primary" => "x-codex-primary-window-minutes",
                _ => "x-codex-secondary-window-minutes",
            },
            seconds / 60,
        );
    }
    if let Some(reset) = value.get("reset_at").and_then(Value::as_i64) {
        insert(
            headers,
            match prefix {
                "primary" => "x-codex-primary-reset-at",
                _ => "x-codex-secondary-reset-at",
            },
            reset,
        );
    }
}

pub fn apply_response_headers(headers: &mut HeaderMap, value: &Value) {
    let rate = value.get("rate_limit");
    mirror_window(
        headers,
        "primary",
        rate.and_then(|item| item.get("primary_window")),
    );
    mirror_window(
        headers,
        "secondary",
        rate.and_then(|item| item.get("secondary_window")),
    );
    headers.insert(
        axum::http::header::CACHE_CONTROL,
        HeaderValue::from_static("private, max-age=60"),
    );
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalizes_subscription_windows_and_drops_unknown_fields() {
        let raw = json!({
            "plan_type": "pro",
            "account_secret": "must not escape",
            "rate_limit": {
                "allowed": true,
                "limit_reached": false,
                "primary_window": {
                    "used_percent": 12.5,
                    "limit_window_seconds": 18000,
                    "reset_after_seconds": 9000,
                    "reset_at": 1900000000
                },
                "secondary_window": {
                    "used_percent": 40.0,
                    "limit_window_seconds": 604800,
                    "reset_after_seconds": 400000,
                    "reset_at": 1900400000
                }
            },
            "credits": {"unlimited": false, "balance": "12.5", "private": "no"}
        });
        let value = normalize_usage(&raw).unwrap();
        assert_eq!(value["plan_type"], "pro");
        assert_eq!(
            value["rate_limit"]["primary_window"]["remaining_percent"],
            87.5
        );
        assert_eq!(
            value["rate_limit"]["secondary_window"]["limit_window_seconds"],
            604800
        );
        assert!(value.get("account_secret").is_none());
        assert!(value["credits"].get("private").is_none());
    }

    #[test]
    fn mirrors_the_official_codex_header_family() {
        let value = normalize_usage(&json!({
            "rate_limit": {
                "primary_window": {"used_percent": 12.5, "limit_window_seconds": 18000, "reset_at": 1900000000},
                "secondary_window": {"used_percent": 40.0, "limit_window_seconds": 604800, "reset_at": 1900400000}
            }
        })).unwrap();
        let mut headers = HeaderMap::new();
        apply_response_headers(&mut headers, &value);
        assert_eq!(headers["x-codex-primary-used-percent"], "12.5");
        assert_eq!(headers["x-codex-primary-window-minutes"], "300");
        assert_eq!(headers["x-codex-secondary-window-minutes"], "10080");
        assert_eq!(headers["x-codex-secondary-reset-at"], "1900400000");
    }

    #[test]
    fn missing_values_remain_unknown_instead_of_becoming_zero() {
        let value = normalize_usage(&json!({})).unwrap();
        assert!(value["rate_limit"]["primary_window"]["used_percent"].is_null());
        assert!(value["credits"]["unlimited"].is_null());
    }
}
