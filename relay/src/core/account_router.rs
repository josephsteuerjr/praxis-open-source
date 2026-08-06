//! Two-slot ChatGPT subscription routing.
//!
//! The router deliberately has a very small policy surface: the currently
//! active slot is sticky and persisted, and only a confirmed subscription
//! exhaustion signal may move it to the other configured slot.

use anyhow::{anyhow, bail, Context, Result};
use chrono::Utc;
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;
use tempfile::NamedTempFile;
use tokio::sync::Mutex;
use tracing::{info, warn};

use crate::login::lib::{AuthMode, CodexAuth};

const STATE_SCHEMA: &str = "relay.account-router.v1";
const ACCOUNT_SLOTS: [&str; 2] = ["primary", "secondary"];
const DEFAULT_COOLDOWN_SECONDS: u64 = 15 * 60;

#[derive(Clone)]
pub struct AccountRouter {
    accounts: Arc<Vec<AccountProfile>>,
    state: Arc<Mutex<PersistentState>>,
    state_file: Arc<PathBuf>,
    cooldown: Duration,
}

struct AccountProfile {
    slot: String,
    auth_home: PathBuf,
    refresh_lock: Mutex<()>,
}

/// One internally consistent credential snapshot.  It intentionally has no
/// `Debug` implementation so bearer tokens cannot be logged by accident.
pub struct AccountLease {
    pub slot: String,
    pub access_token: String,
    pub account_id: String,
}

#[derive(Clone, Deserialize, Serialize)]
struct PersistentState {
    schema: String,
    active: String,
    #[serde(default)]
    exhausted_until: BTreeMap<String, i64>,
}

impl AccountRouter {
    pub async fn load(auth_root: &Path) -> Result<Self> {
        let accounts_root = auth_root.join("accounts");
        let mut accounts = Vec::new();
        for slot in ACCOUNT_SLOTS {
            let auth_home = accounts_root.join(slot);
            if auth_home.join("auth.json").is_file() {
                accounts.push(AccountProfile {
                    slot: slot.to_string(),
                    auth_home,
                    refresh_lock: Mutex::new(()),
                });
            }
        }

        // Backward compatibility keeps the current deployment viable while the
        // first profile is migrated into accounts/primary.
        if accounts.is_empty() && auth_root.join("auth.json").is_file() {
            accounts.push(AccountProfile {
                slot: "primary".to_string(),
                auth_home: auth_root.to_path_buf(),
                refresh_lock: Mutex::new(()),
            });
        }
        if accounts.is_empty() {
            bail!("no ChatGPT account profiles are configured");
        }

        let state_file = std::env::var_os("RELAY_ACCOUNT_STATE_FILE")
            .map(PathBuf::from)
            .unwrap_or_else(|| auth_root.join("router-state.json"));
        let first_slot = accounts[0].slot.clone();
        let mut state = if state_file.exists() {
            let bytes = fs::read(&state_file).context("failed to read account router state")?;
            let state: PersistentState =
                serde_json::from_slice(&bytes).context("invalid account router state")?;
            if state.schema != STATE_SCHEMA {
                bail!("unsupported account router state schema");
            }
            state
        } else {
            PersistentState {
                schema: STATE_SCHEMA.to_string(),
                active: first_slot.clone(),
                exhausted_until: BTreeMap::new(),
            }
        };
        if !accounts.iter().any(|account| account.slot == state.active) {
            warn!("persisted account slot is unavailable; selecting the first configured slot");
            state.active = first_slot;
        }

        let cooldown_seconds = std::env::var("RELAY_ACCOUNT_COOLDOWN_SECONDS")
            .ok()
            .and_then(|value| value.trim().parse::<u64>().ok())
            .unwrap_or(DEFAULT_COOLDOWN_SECONDS)
            .clamp(60, 7 * 24 * 60 * 60);
        let router = Self {
            accounts: Arc::new(accounts),
            state: Arc::new(Mutex::new(state.clone())),
            state_file: Arc::new(state_file),
            cooldown: Duration::from_secs(cooldown_seconds),
        };
        router.persist(&state)?;

        // Fail startup if the selected profile itself is unusable.  A broken
        // standby does not take down an otherwise healthy primary.
        router
            .lease_active()
            .await
            .context("active ChatGPT account is unusable")?;
        info!(
            "account router ready: active={}, configured={}",
            router.active_slot().await,
            router.account_count()
        );
        Ok(router)
    }

    pub fn account_count(&self) -> usize {
        self.accounts.len()
    }

    pub async fn active_slot(&self) -> String {
        self.state.lock().await.active.clone()
    }

    pub async fn lease_active(&self) -> Result<AccountLease> {
        let slot = self.active_slot().await;
        self.lease_slot(&slot).await
    }

    /// Atomically marks `exhausted` unavailable for a bounded cooldown and
    /// makes the other valid account the global active slot.  Concurrent
    /// requests that saw the same exhaustion converge on the first switch.
    pub async fn switch_after_quota(&self, exhausted: &AccountLease) -> Result<AccountLease> {
        let now = Utc::now().timestamp();
        let candidate = {
            let state = self.state.lock().await;
            if state.active != exhausted.slot {
                let active = state.active.clone();
                drop(state);
                return self.lease_slot(&active).await;
            }
            self.accounts
                .iter()
                .find(|account| {
                    account.slot != exhausted.slot
                        && state
                            .exhausted_until
                            .get(&account.slot)
                            .copied()
                            .unwrap_or(0)
                            <= now
                })
                .map(|account| account.slot.clone())
                .ok_or_else(|| anyhow!("no standby subscription is currently available"))?
        };

        // Validate and refresh the standby before changing global state.
        let standby = self
            .lease_slot(&candidate)
            .await
            .context("standby ChatGPT account is unusable")?;
        if standby.account_id == exhausted.account_id {
            bail!("standby profile resolves to the exhausted ChatGPT account");
        }

        let mut state = self.state.lock().await;
        if state.active != exhausted.slot {
            let active = state.active.clone();
            drop(state);
            return self.lease_slot(&active).await;
        }
        let mut next = state.clone();
        next.active = standby.slot.clone();
        next.exhausted_until
            .insert(exhausted.slot.clone(), now + self.cooldown.as_secs() as i64);
        self.persist(&next)?;
        *state = next;
        info!(
            "subscription quota exhausted: switched active slot {} -> {}",
            exhausted.slot, standby.slot
        );
        Ok(standby)
    }

    async fn lease_slot(&self, slot: &str) -> Result<AccountLease> {
        let account = self
            .accounts
            .iter()
            .find(|account| account.slot == slot)
            .ok_or_else(|| anyhow!("account slot is not configured"))?;
        // CodexAuth may refresh and rewrite auth.json.  Serialize that operation
        // per account so concurrent requests never race a token refresh.
        let _refresh_guard = account.refresh_lock.lock().await;
        let auth = CodexAuth::from_codex_home(&account.auth_home)
            .context("failed to load ChatGPT authentication")?
            .ok_or_else(|| anyhow!("ChatGPT authentication is not configured"))?;
        if auth.mode != AuthMode::ChatGPT {
            bail!("ChatGPT subscription authentication is required");
        }
        let tokens = auth
            .get_token_data()
            .await
            .context("failed to refresh ChatGPT authentication")?;
        if tokens.access_token.trim().is_empty() {
            bail!("ChatGPT access token is empty");
        }
        let account_id = tokens
            .account_id
            .filter(|value| !value.trim().is_empty())
            .ok_or_else(|| anyhow!("ChatGPT account id is missing"))?;
        Ok(AccountLease {
            slot: account.slot.clone(),
            access_token: tokens.access_token,
            account_id,
        })
    }

    fn persist(&self, state: &PersistentState) -> Result<()> {
        let parent = self
            .state_file
            .parent()
            .ok_or_else(|| anyhow!("account router state path has no parent"))?;
        fs::create_dir_all(parent).context("failed to create account router state directory")?;
        let mut temp = NamedTempFile::new_in(parent)
            .context("failed to create temporary account router state")?;
        serde_json::to_writer_pretty(temp.as_file_mut(), state)
            .context("failed to serialize account router state")?;
        temp.as_file_mut()
            .write_all(b"\n")
            .context("failed to terminate account router state")?;
        temp.as_file_mut()
            .sync_all()
            .context("failed to sync account router state")?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            temp.as_file()
                .set_permissions(fs::Permissions::from_mode(0o600))
                .context("failed to protect account router state")?;
        }
        temp.persist(self.state_file.as_ref())
            .map_err(|error| error.error)
            .context("failed to atomically replace account router state")?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use base64::Engine;
    use serde_json::json;
    use tempfile::tempdir;

    fn write_account(root: &Path, slot: &str, account_id: &str) {
        let home = root.join("accounts").join(slot);
        fs::create_dir_all(&home).unwrap();
        let payload = json!({
            "email": format!("{slot}@example.test"),
            "https://api.openai.com/auth": {"chatgpt_plan_type": "pro"}
        });
        let encoded = base64::engine::general_purpose::URL_SAFE_NO_PAD
            .encode(serde_json::to_vec(&payload).unwrap());
        let jwt = format!("e30.{encoded}.c2ln");
        let auth = json!({
            "OPENAI_API_KEY": null,
            "tokens": {
                "id_token": jwt,
                "access_token": format!("access-{slot}"),
                "refresh_token": format!("refresh-{slot}"),
                "account_id": account_id
            },
            "last_refresh": Utc::now().to_rfc3339()
        });
        fs::write(
            home.join("auth.json"),
            serde_json::to_vec_pretty(&auth).unwrap(),
        )
        .unwrap();
    }

    #[tokio::test]
    async fn quota_switch_is_global_sticky_and_persistent() {
        let root = tempdir().unwrap();
        write_account(root.path(), "primary", "acct-primary");
        write_account(root.path(), "secondary", "acct-secondary");
        let router = AccountRouter::load(root.path()).await.unwrap();
        let exhausted = router.lease_active().await.unwrap();
        let standby = router.switch_after_quota(&exhausted).await.unwrap();
        assert_eq!(standby.slot, "secondary");
        assert_eq!(router.lease_active().await.unwrap().slot, "secondary");

        let reloaded = AccountRouter::load(root.path()).await.unwrap();
        assert_eq!(reloaded.lease_active().await.unwrap().slot, "secondary");
    }

    #[tokio::test]
    async fn duplicate_subscription_is_refused() {
        let root = tempdir().unwrap();
        write_account(root.path(), "primary", "same-account");
        write_account(root.path(), "secondary", "same-account");
        let router = AccountRouter::load(root.path()).await.unwrap();
        let exhausted = router.lease_active().await.unwrap();
        let error = match router.switch_after_quota(&exhausted).await {
            Ok(_) => panic!("duplicate subscription was accepted"),
            Err(error) => error,
        };
        assert!(error.to_string().contains("exhausted ChatGPT account"));
        assert_eq!(router.active_slot().await, "primary");
    }
}
