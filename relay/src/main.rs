use axum::{
    extract::{rejection::JsonRejection, DefaultBodyLimit, State},
    http::{HeaderMap, StatusCode},
    response::{sse::Event, IntoResponse, Json, Response},
    routing::{get, post},
    Router,
};
use std::net::SocketAddr;
use std::sync::Arc;
use tokio_stream::StreamExt;
use tower_http::cors::CorsLayer;
use tracing::{error, info, warn};
use tracing_appender;
use tracing_subscriber::{layer::SubscriberExt, util::SubscriberInitExt};

// Modules
mod core;
mod login;

use core::chat_completions;
use core::config::Config;
use core::limits::LimitsCache;
use core::models::{
    is_supported_model, supported_model_list, ChatRequest, ModelList, MAX_CHAT_REQUEST_BYTES,
    SUPPORTED_MODELS,
};
use login::lib::CodexAuth;

// For CLI menu
use std::io::{self, Write};
use std::sync::Mutex;
use tokio::task::JoinHandle;

// Global registry for server handles
use once_cell::sync::Lazy;
static SERVER_HANDLES: Lazy<Mutex<Vec<JoinHandle<()>>>> = Lazy::new(|| Mutex::new(Vec::new()));

#[derive(Clone)]
struct AppState {
    config: Arc<Config>,
    limits: LimitsCache,
    // One pooled client for every upstream call: Client::new() per request cost a
    // fresh DNS+TCP+TLS handshake to chatgpt.com on every single LLM call.
    client: reqwest::Client,
}

#[tokio::main]
async fn main() {
    // The container runs from /app, whose logs directory is the persisted
    // compose mount. RELAY_LOG_DIR keeps non-container deployments explicit.
    let logs_dir = std::env::var_os("RELAY_LOG_DIR")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|| {
            std::env::current_dir()
                .unwrap_or_else(|_| std::path::PathBuf::from("."))
                .join("logs")
        });
    if let Err(e) = std::fs::create_dir_all(&logs_dir) {
        eprintln!("Failed to create logs directory: {}", e);
    }
    // Initialize tracing with both console and file output
    let file_appender = tracing_appender::rolling::daily(logs_dir.clone(), "relay.log");
    let (non_blocking, _guard) = tracing_appender::non_blocking(file_appender);

    tracing_subscriber::registry()
        .with(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "codex_proxy=info,tower_http=info".into()),
        )
        .with(
            tracing_subscriber::fmt::layer()
                .with_writer(std::io::stdout)
                .with_ansi(true)
                .with_target(false)
                .with_thread_ids(false)
                .with_thread_names(false),
        )
        .with(
            tracing_subscriber::fmt::layer()
                .with_writer(non_blocking)
                .with_ansi(false)
                .with_target(true)
                .with_thread_ids(true)
                .with_thread_names(true),
        )
        .init();

    info!("=== Starting Codex Proxy Server ==="); // Codex Proxy Server
    info!("Log directory: {}", logs_dir.display());
    info!("Timestamp: {}", chrono::Utc::now().to_rfc3339());

    // Display CLI menu
    loop {
        display_menu();
        let choice = get_user_choice();

        match choice.as_str() {
            "1" => {
                if let Err(e) = run_server().await {
                    error!("Failed to start server: {}", e);
                }
            }
            "2" => {
                // Close all servers functionality
                if let Err(e) = close_all_servers().await {
                    error!("Failed to close servers: {}", e);
                }
            }
            "3" => {
                if let Err(e) = run_login().await {
                    error!("Login failed: {}", e);
                }
            }
            "4" => {
                if let Err(e) = refresh_token().await {
                    error!("Token refresh failed: {}", e);
                }
            }
            "5" => {
                println!("Exiting...");
                break;
            }
            "6" => {
                if let Err(e) = list_running_servers().await {
                    error!("Failed to list running servers: {}", e);
                }
            }
            _ => {
                println!("Invalid choice. Please try again.");
            }
        }
    }
}

async fn run_login() -> anyhow::Result<()> {
    info!("Starting login process");
    let home_dir =
        dirs::home_dir().ok_or_else(|| anyhow::anyhow!("Could not determine home directory"))?;
    let codex_home = home_dir.join(".codex");
    let opencode_home = home_dir.join(".opencode");
    let codex_auth_path = codex_home.join("auth.json");
    let opencode_auth_path = opencode_home.join("auth.json");

    // Try to read or create in .codex first
    std::fs::create_dir_all(&codex_home)?;
    println!("Codex home directory: {:?}", codex_home);
    println!("Expected auth file path: {:?}", codex_auth_path);

    let mut used_opencode = false;
    let login_result = login::lib::login_with_chatgpt(&codex_home, false).await;
    if login_result.is_err() || !codex_auth_path.exists() {
        // If failed or file not created, try .opencode
        println!("Could not create or find auth.json in .codex, switching to .opencode directory (Opencode integration)...");
        std::fs::create_dir_all(&opencode_home)?;
        let login_result2 = login::lib::login_with_chatgpt(&opencode_home, false).await;
        if login_result2.is_err() || !opencode_auth_path.exists() {
            // Third fallback: create ./local_auth directory in current working directory
            let local_auth_dir = std::env::current_dir()?.join("local_auth");
            std::fs::create_dir_all(&local_auth_dir)?;
            let local_auth_path = local_auth_dir.join("auth.json");
            println!("Could not create or find auth.json in .codex or .opencode, switching to ./local_auth directory (local fallback)...");
            let login_result3 = login::lib::login_with_chatgpt(&local_auth_dir, false).await;
            if login_result3.is_err() || !local_auth_path.exists() {
                return Err(anyhow::anyhow!("Login failed: Could not create auth.json in .codex, .opencode, or ./local_auth directory."));
            }
            println!("Auth file created successfully at: {:?} (local fallback, move to ~/.codex or ~/.opencode for best compatibility)", local_auth_path);
            println!("WARNING: Using local fallback directory for authentication. Move auth.json to ~/.codex or ~/.opencode for best compatibility and Opencode integration.");
            return Ok(());
        }
        used_opencode = true;
    }

    if used_opencode {
        println!(
            "Auth file created successfully at: {:?} (Opencode integration)",
            opencode_auth_path
        );
    } else {
        println!(
            "Auth file created successfully at: {:?} (Codex Proxy Server)",
            codex_auth_path
        );
    }

    info!("Login successful");
    println!("Login completed!");
    Ok(())
}

fn display_menu() {
    println!("\n=== Codex Proxy Server===");
    println!("1. Run server");
    println!("2. Close all servers");
    println!("3. Login");
    println!("4. Refresh token");
    println!("5. Exit");
    println!("6. List running servers");
    print!("Please select an option (1-6): ");
    io::stdout().flush().unwrap();
}

fn get_user_choice() -> String {
    let mut choice = String::new();
    io::stdin()
        .read_line(&mut choice)
        .expect("Failed to read input");
    choice.trim().to_string()
}

async fn run_server() -> anyhow::Result<()> {
    info!("Starting Codex Proxy Server");

    // Load configuration
    let config = match Config::load() {
        Ok(config) => {
            info!("Configuration loaded successfully");
            Arc::new(config)
        }
        Err(e) => {
            error!("Failed to load configuration: {}", e);
            return Err(e);
        }
    };

    // Check authentication
    match check_authentication(&config).await {
        Ok(_) => info!("Authentication check passed"),
        Err(e) => {
            error!("Authentication check failed: {}", e);
            error!("Please use the 'Login' option in the CLI menu. This will enable Opencode and other integrations.");
            return Err(e);
        }
    }

    // Create app state
    let client = reqwest::Client::builder()
        .tcp_keepalive(std::time::Duration::from_secs(30))
        .pool_idle_timeout(std::time::Duration::from_secs(90))
        .build()
        .unwrap_or_else(|error| {
            warn!("shared client builder failed ({}), falling back to default", error);
            reqwest::Client::new()
        });
    let app_state = AppState {
        config,
        limits: LimitsCache::default(),
        client,
    };

    // Create router
    let app = Router::new()
        .route(
            "/chat/completions",
            post(chat_completions_handler).layer(DefaultBodyLimit::max(MAX_CHAT_REQUEST_BYTES)),
        )
        .route("/v1/models", get(models_handler))
        .route("/v1/limits", get(limits_handler))
        .route("/health", get(health_handler))
        .layer(CorsLayer::permissive())
        .with_state(app_state);

    // Configure server
    let addr = SocketAddr::from(([127, 0, 0, 1], 5011));
    info!("Server listening on {}", addr);

    // Start server and block until it exits
    let listener = tokio::net::TcpListener::bind(addr).await.unwrap();
    println!("Server is running. Press Ctrl+C to stop.");
    axum::serve(listener, app).await.unwrap();
    Ok(())
}

async fn refresh_token() -> anyhow::Result<()> {
    println!("Refreshing token...");

    // Load configuration
    let config = Config::load()?;

    // Get the codex auth
    let codex_auth = match CodexAuth::from_codex_home(&config.codex_home) {
        Ok(Some(auth)) => auth,
        _ => {
            return Err(anyhow::anyhow!("No authentication found. Please use the 'Login' option in the CLI menu. This enables Opencode and other integrations."));
        }
    };

    // Get token data which will automatically refresh if needed
    let token_data = match codex_auth.get_token_data().await {
        Ok(data) => data,
        Err(_) => {
            return Err(anyhow::anyhow!("No authentication found. Please use the 'Login' option in the CLI menu. This enables Opencode and other integrations."));
        }
    };

    println!("Token refreshed successfully!");
    match &token_data.account_id {
        Some(account_id) => println!("Account ID: {}", account_id),
        None => println!("Account ID: None"),
    }

    Ok(())
}

async fn close_all_servers() -> anyhow::Result<()> {
    println!("Closing all servers (system-wide)...");
    let mut closed = 0;
    for port in 5011..=5020 {
        let pids = get_pids_for_port(port);
        for pid in pids {
            if kill_pid(pid) {
                println!("Killed server on port {} (PID {})", port, pid);
                closed += 1;
            }
        }
    }
    println!("Closed {} running server(s) on ports 5011-5020.", closed);
    Ok(())
}

// Get PIDs listening on a port
fn get_pids_for_port(port: u16) -> Vec<u32> {
    #[cfg(target_family = "unix")]
    {
        use std::process::Command;
        let output = Command::new("lsof")
            .arg("-ti")
            .arg(format!(":{}", port))
            .output();
        if let Ok(out) = output {
            let stdout = String::from_utf8_lossy(&out.stdout);
            stdout
                .lines()
                .filter_map(|line| line.trim().parse::<u32>().ok())
                .collect()
        } else {
            vec![]
        }
    }
    #[cfg(target_family = "windows")]
    {
        use std::process::Command;
        let output = Command::new("netstat").arg("-ano").output();
        if let Ok(out) = output {
            let stdout = String::from_utf8_lossy(&out.stdout);
            stdout
                .lines()
                .filter_map(|line| {
                    if line.contains(&format!(":{}", port)) {
                        line.split_whitespace().last()?.parse::<u32>().ok()
                    } else {
                        None
                    }
                })
                .collect()
        } else {
            vec![]
        }
    }
}

// Kill a process by PID
fn kill_pid(pid: u32) -> bool {
    #[cfg(target_family = "unix")]
    {
        use std::process::Command;
        let status = Command::new("kill").arg("-9").arg(pid.to_string()).status();
        status.map(|s| s.success()).unwrap_or(false)
    }
    #[cfg(target_family = "windows")]
    {
        use std::process::Command;
        let status = Command::new("taskkill")
            .arg("/PID")
            .arg(pid.to_string())
            .arg("/F")
            .status();
        status.map(|s| s.success()).unwrap_or(false)
    }
}

// Utility: Check if a port is in use (cross-platform)
fn is_port_in_use(port: u16) -> bool {
    #[cfg(target_family = "unix")]
    {
        use std::process::Command;
        // Try lsof first
        let lsof_output = Command::new("lsof")
            .arg("-i")
            .arg(format!(":{}", port))
            .output();
        if let Ok(out) = lsof_output {
            let stdout = String::from_utf8_lossy(&out.stdout);
            if stdout.contains(&format!(":{}", port)) {
                return true;
            }
        } else {
            eprintln!("lsof failed for port {}", port);
        }
        // Fallback to netstat
        let netstat_output = Command::new("netstat").arg("-an").output();
        if let Ok(out) = netstat_output {
            let stdout = String::from_utf8_lossy(&out.stdout);
            if stdout.contains(&format!(":{}", port)) {
                return true;
            }
        } else {
            eprintln!("netstat failed for port {}", port);
        }
        false
    }
    #[cfg(target_family = "windows")]
    {
        use std::process::Command;
        let output = Command::new("netstat").arg("-ano").output();
        if let Ok(out) = output {
            let stdout = String::from_utf8_lossy(&out.stdout);
            if stdout.contains(&format!(":{}", port)) {
                return true;
            }
        } else {
            eprintln!("netstat failed for port {}", port);
        }
        false
    }
}

async fn list_running_servers() -> anyhow::Result<()> {
    println!("Checking ports 5011-5020 for running servers...");
    let mut found = false;
    for port in 5011..=5020 {
        if is_port_in_use(port) {
            println!("Port {}: RUNNING", port);
            found = true;
        }
    }
    if !found {
        println!("No running servers found on ports 5011-5020.");
    }
    Ok(())
}

async fn check_authentication(config: &Config) -> anyhow::Result<()> {
    info!(
        "Checking authentication in directory: {:?}",
        &config.codex_home
    );
    let auth_file_path = config.codex_home.join("auth.json");
    info!("Looking for auth file at: {:?}", auth_file_path);

    if auth_file_path.exists() {
        info!("Auth file found!");
        // Try to read the file to check if it's valid
        match std::fs::read_to_string(&auth_file_path) {
            Ok(content) => {
                // Auth file content preview removed for security
            }
            Err(e) => {
                error!("Error reading auth file: {}", e);
                return Err(anyhow::anyhow!("Failed to read auth file: {}", e));
            }
        }
    } else {
        warn!("Auth file not found!");
        // List files in the directory to see what's there
        if let Ok(entries) = std::fs::read_dir(&config.codex_home) {
            info!("Files in codex home directory:");
            for entry in entries {
                if let Ok(entry) = entry {
                    info!("  - {}", entry.file_name().to_string_lossy());
                }
            }
        }

        // Check if we're in .codex or .opencode and provide specific guidance
        if let Some(home_dir) = dirs::home_dir() {
            let codex_path = home_dir.join(".codex");
            let opencode_path = home_dir.join(".opencode");

            if config.codex_home == codex_path {
                info!("Looking in .codex directory. Checking if auth file exists in .opencode...");
                let opencode_auth = opencode_path.join("auth.json");
                if opencode_auth.exists() {
                    info!("Found auth file in .opencode directory. Consider moving it to .codex for better compatibility.");
                }
            } else if config.codex_home == opencode_path {
                info!("Looking in .opencode directory. Checking if auth file exists in .codex...");
                let codex_auth = codex_path.join("auth.json");
                if codex_auth.exists() {
                    info!("Found auth file in .codex directory. Using that instead.");
                }
            }
        }
    }

    let codex_auth = match CodexAuth::from_codex_home(&config.codex_home) {
        Ok(Some(auth)) => auth,
        _ => {
            return Err(anyhow::anyhow!("No authentication found. Please use the 'Login' option in the CLI menu. This enables Opencode and other integrations."));
        }
    };

    let token_data = match codex_auth.get_token_data().await {
        Ok(data) => data,
        Err(_) => {
            return Err(anyhow::anyhow!("No authentication found. Please use the 'Login' option in the CLI menu. This enables Opencode and other integrations."));
        }
    };

    if token_data.access_token.is_empty() {
        return Err(anyhow::anyhow!("No authentication found. Please use the 'Login' option in the CLI menu. This enables Opencode and other integrations."));
    }

    if token_data.account_id.is_none() {
        return Err(anyhow::anyhow!("No authentication found. Please use the 'Login' option in the CLI menu. This enables Opencode and other integrations."));
    }

    // Log token information for debugging
    info!("Authentication successful");
    info!(
        "Plan type: {}",
        codex_auth.get_plan_type().as_deref().unwrap_or("None")
    );

    Ok(())
}

async fn health_handler() -> Json<serde_json::Value> {
    info!("💓 Health check endpoint requested");
    let response = serde_json::json!({
        "status": "healthy",
        "timestamp": chrono::Utc::now().to_rfc3339(),
        "service": "relay",
        "version": env!("CARGO_PKG_VERSION")
    });
    info!("✅ Health check response: {}", response);
    Json(response)
}

async fn models_handler(State(_state): State<AppState>) -> Json<ModelList> {
    info!("📋 Models endpoint requested");
    // Built from core::models::SUPPORTED_MODELS — the single source of truth also
    // used by the request validator below, so the two can never disagree.
    let list = supported_model_list();
    info!("✅ Returning {} available models", list.data.len());
    Json(list)
}

async fn limits_handler(State(state): State<AppState>) -> Response {
    match state.limits.get(&state.config).await {
        Ok(value) => {
            let mut response = Json(value.clone()).into_response();
            core::limits::apply_response_headers(response.headers_mut(), &value);
            response
        }
        Err(error) => {
            warn!("Limits request failed: {}", error);
            (
                StatusCode::BAD_GATEWAY,
                Json(serde_json::json!({
                    "error": {
                        "message": "OpenAI usage limits are temporarily unavailable",
                        "type": "upstream_error",
                        "code": "limits_unavailable"
                    }
                })),
            )
                .into_response()
        }
    }
}

async fn chat_completions_handler(
    State(state): State<AppState>,
    _headers: HeaderMap,
    payload: Result<Json<ChatRequest>, JsonRejection>,
) -> Result<Response, StatusCode> {
    let request = match payload {
        Ok(Json(request)) => request,
        Err(rejection) => {
            let status = rejection.status();
            let code = if status == StatusCode::PAYLOAD_TOO_LARGE {
                "request_too_large"
            } else {
                "invalid_json"
            };
            return Ok((
                status,
                Json(serde_json::json!({
                    "error": {
                        "message": rejection.body_text(),
                        "type": "invalid_request_error",
                        "param": null,
                        "code": code
                    }
                })),
            )
                .into_response());
        }
    };

    info!("🚀 CHAT COMPLETIONS REQUEST RECEIVED!");
    info!(
        "Request model supported: {}",
        is_supported_model(&request.model)
    );
    info!("Request messages count: {}", request.messages.len());
    info!("Request tools count: {}", request.tools.len());
    info!("Request image parts count: {}", request.image_part_count());

    // Validate model against the single source of truth (core::models::SUPPORTED_MODELS),
    // not a prefix — the old starts_with("gpt-5") let bogus slugs through and would
    // reject future models. The 404 message lists the real supported set.
    if !is_supported_model(&request.model) {
        warn!("Invalid model requested (value redacted)");
        return Ok((
            StatusCode::NOT_FOUND,
            Json(serde_json::json!({
                "error": {
                    "message": format!("Model not found. Supported models: {}. Requested: {}", SUPPORTED_MODELS.join(", "), request.model),
                    "type": "model_not_found",
                    "code": "model_not_found"
                }
            }))
        ).into_response());
    }

    if let Err(validation_error) = request.validate_content() {
        warn!(
            "Invalid message content at {}: {}",
            validation_error.param, validation_error.message
        );
        return Ok((
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({
                "error": {
                    "message": validation_error.to_string(),
                    "type": "invalid_request_error",
                    "param": validation_error.param,
                    "code": "invalid_message_content"
                }
            })),
        )
            .into_response());
    }

    // Process the chat completion
    match chat_completions::stream_chat_completions(&state.config, request, state.client.clone())
        .await
    {
        Ok(response_stream) => {
            info!("✅ Chat completion stream started successfully");
            // Convert the response stream to SSE
            let sse_stream = tokio_stream::wrappers::ReceiverStream::new(response_stream)
                .map(|result| {
                    match result {
                        Ok(event) => {
                            let json = serde_json::to_string(&event).unwrap_or_else(|e| {
                                error!("Failed to serialize event: {}", e);
                                r#"{"error": "Failed to serialize event"}"#.to_string()
                            });
                            Ok::<Event, Box<dyn std::error::Error + Send + Sync>>(Event::default().data(json))
                        }
                        Err(e) => {
                            error!("Stream error: {}", e);
                            let error_json = serde_json::to_string(&serde_json::json!({
                                "error": {
                                    "message": format!("Stream error: {}", e),
                                    "type": "stream_error",
                                    "code": "stream_error"
                                }
                            })).unwrap_or_else(|_| r#"{"error":{"message":"Failed to format error","type":"format_error","code":"format_error"}}"#.to_string());
                            Ok::<Event, Box<dyn std::error::Error + Send + Sync>>(Event::default().data(error_json))
                        }
                    }
                });

            Ok(axum::response::Sse::new(sse_stream).into_response())
        }
        Err(e) => {
            error!("❌ Chat completions error: {}", e);
            error!("Error details: {:?}", e);
            Ok((
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(serde_json::json!({
                    "error": {
                        "message": format!("Failed to process chat completion: {}", e),
                        "type": "server_error",
                        "code": "internal_error"
                    }
                })),
            )
                .into_response())
        }
    }
}
