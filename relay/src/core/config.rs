use std::path::PathBuf;
use dirs::home_dir;

#[derive(Debug, Clone, PartialEq)]
pub struct Config {
    pub codex_home: PathBuf,
    pub chatgpt_base_url: String,
    pub model: String,
    pub user_instructions: Option<String>,
    pub reasoning_effort: Option<String>,
    pub instructions_mode: Option<String>,
    pub parallel_tool_calls: bool,
}

impl Config {
    pub fn load() -> anyhow::Result<Self> {
        let codex_home = find_codex_home();

        // Load user instructions from AGENTS.md
        let user_instructions = Self::load_instructions(Some(&codex_home));
        let reasoning_effort = Self::load_reasoning_effort();
        let instructions_mode = Self::load_instructions_mode();
        let parallel_tool_calls = Self::load_parallel_tool_calls();

        // Check if auth.json exists in ./local_auth directory (fallback)
        let local_auth_dir = std::env::current_dir()?.join("local_auth");
        let local_auth_file = local_auth_dir.join("auth.json");
        if local_auth_file.exists() {
            return Ok(Config {
                codex_home: local_auth_dir,
                chatgpt_base_url: "https://chatgpt.com/backend-api/codex".to_string(),
                model: "gpt-5.4".to_string(),
                user_instructions,
                reasoning_effort,
                instructions_mode: instructions_mode.clone(),
                parallel_tool_calls,
            });
        }
        // Check if auth.json exists in the current directory (legacy fallback)
        let current_dir_auth = std::env::current_dir()?.join("auth.json");
        if current_dir_auth.exists() {
            return Ok(Config {
                codex_home: std::env::current_dir()?,
                chatgpt_base_url: "https://chatgpt.com/backend-api/codex".to_string(),
                model: "gpt-5.4".to_string(),
                user_instructions,
                reasoning_effort,
                instructions_mode: instructions_mode.clone(),
                parallel_tool_calls,
            });
        }

        Ok(Config {
            codex_home,
            chatgpt_base_url: "https://chatgpt.com/backend-api/codex".to_string(),
            model: "gpt-5.4".to_string(), // Default, but can be changed to any gpt-5* variant
            user_instructions,
            reasoning_effort,
            instructions_mode,
            parallel_tool_calls,
        })
    }

    /// RELAY_INSTRUCTIONS: "codex" (default — full vendored prompt.md, known-accepted
    /// by the backend) or "minimal" — a ~60-word neutral stub instead of ~5k tokens of
    /// coding-agent instructions per call.  When minimal is rejected upstream (4xx) the
    /// request is retried once with the full prompt, so the worst case is the default.
    pub fn minimal_instructions(&self) -> bool {
        self.instructions_mode.as_deref() == Some("minimal")
    }

    fn load_instructions_mode() -> Option<String> {
        std::env::var("RELAY_INSTRUCTIONS").ok().map(|v| v.trim().to_lowercase())
    }

    /// RELAY_PARALLEL_TOOL_CALLS: unset/"true" lets the model batch several tool calls
    /// in one response (the only client executes batched tool_use natively); "false"
    /// restores the old one-tool-per-round-trip behavior.
    fn load_parallel_tool_calls() -> bool {
        !matches!(
            std::env::var("RELAY_PARALLEL_TOOL_CALLS").ok().as_deref().map(str::trim),
            Some("false") | Some("0") | Some("off")
        )
    }

    /// RELAY_REASONING_EFFORT: none|minimal|low|medium|high|xhigh, applied when a
    /// request carries no reasoning_effort of its own.  "passthrough"/"default"/""
    /// omit the field entirely (upstream default, currently medium).  Env unset
    /// keeps today's behavior: reasoning disabled ("none") on every call.
    fn load_reasoning_effort() -> Option<String> {
        match std::env::var("RELAY_REASONING_EFFORT") {
            Ok(value) => {
                let value = value.trim().to_lowercase();
                if value.is_empty() || value == "passthrough" || value == "default" {
                    None
                } else {
                    Some(value)
                }
            }
            Err(_) => Some("none".to_string()),
        }
    }

    fn load_instructions(codex_dir: Option<&std::path::Path>) -> Option<String> {
        let mut p = match codex_dir {
            Some(p) => p.to_path_buf(),
            None => return None,
        };

        p.push("AGENTS.md");
        std::fs::read_to_string(&p).ok().and_then(|s| {
            let s = s.trim();
            if s.is_empty() {
                None
            } else {
                Some(s.to_string())
            }
        })
    }
}

fn find_codex_home() -> PathBuf {
    // Try to find codex home directory for Codex Proxy Server and Opencode integration
    if let Some(home) = home_dir() {
        // First check for .codex directory (Codex Proxy Server CLI compatibility)
        let codex_home = home.join(".codex");
        if codex_home.exists() {
            return codex_home;
        }
        
        // Then check for .opencode directory (Opencode integration default)
        let opencode_home = home.join(".opencode");
        if opencode_home.exists() {
            return opencode_home;
        }
    }
    
    // Fallback to current directory
    std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."))
}