use std::collections::HashSet;
use std::sync::Arc;
use std::sync::Mutex;
use std::time::Duration;

use anyhow::{Context, Result};
use chrono::Utc;
use praxis_body_protocol::{
    ArtifactRef, CapabilityManifest, Envelope, ExecutionIdentity, ExecutionKind, Frame,
    OperationStatus,
};
use serde::Deserialize;
use serde_json::{Value, json};

use crate::artifact::ArtifactClient;
use crate::config::BodyConfig;
use crate::journal::{Admission, Journal, RequestRecord};
use crate::{compose, desktop, fsops, identity, interactive_router, process, system_router, uia};

pub struct Runtime {
    pub config: BodyConfig,
    journal: Arc<Journal>,
    artifacts: ArtifactClient,
    inflight: Mutex<HashSet<String>>,
}

#[derive(Debug, Deserialize)]
struct OperationArgs {
    operation_id: String,
    #[serde(default = "default_tail")]
    tail: u64,
}

#[derive(Debug, Default, Deserialize)]
struct ProcessListArgs {
    #[serde(default)]
    root: Option<String>,
}

fn default_tail() -> u64 {
    16_384
}

/// Куда уезжает имя глагола. Вынесено из `dispatch` отдельно ровно затем, чтобы это можно
/// было проверить тестом БЕЗ исполнения: «объявленное исполняется, исполнимое объявляется»
/// — иначе очередной глагол умрёт тихо, как умерли `office.com` и `win32.api`, которые
/// стояли в манифесте, но не имели ни одного обработчика.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Route {
    BodyStatus,
    AdapterList,
    Fs,
    Artifact,
    ProcessStart,
    ProcessStatus,
    ProcessCancel,
    ProcessList,
    WindowRead,
    Composed,
    Desktop,
    Unknown,
}

fn route(capability: &str) -> Route {
    match capability {
        "body.status" => Route::BodyStatus,
        "adapter.list" => Route::AdapterList,
        "fs.export" | "fs.import" => Route::Artifact,
        "process.start" => Route::ProcessStart,
        "process.status" => Route::ProcessStatus,
        "process.cancel" => Route::ProcessCancel,
        "process.list" => Route::ProcessList,
        value if value.starts_with("fs.") => Route::Fs,
        // ⚠ ПОРЯДОК. Обе следующие ветки ловят имена, которые начинаются с `desktop.`,
        // и общая ветка ниже увела бы их в `desktop::dispatch`, где их нет: она ответила
        // бы «unknown native desktop capability», то есть глагол, который существует и
        // объявлен, выглядел бы несуществующим.
        value if uia::handles(value) => Route::WindowRead,
        value if compose::handles(value) => Route::Composed,
        value if value.starts_with("desktop.") || value == "os.process.list" => Route::Desktop,
        _ => Route::Unknown,
    }
}

impl Runtime {
    pub fn open(config: BodyConfig) -> Result<Self> {
        std::fs::create_dir_all(&config.state_dir)?;
        let journal = Arc::new(Journal::open(&config.state_dir.join("body.db"))?);
        let artifacts = ArtifactClient::new(&config)?;
        Ok(Self {
            config,
            journal,
            artifacts,
            inflight: Mutex::new(HashSet::new()),
        })
    }

    pub fn manifest(&self) -> CapabilityManifest {
        let current = identity::current().kind;
        identity::manifest_with_routes(
            &self.config.device_id,
            current == ExecutionKind::System && interactive_router::available(&self.config),
            current == ExecutionKind::Interactive && system_router::available(&self.config),
        )
    }

    pub fn journal(&self) -> &Journal {
        &self.journal
    }

    pub async fn handle(&self, envelope: Envelope) -> Frame {
        let Frame::Invoke {
            request_id,
            operation_id,
            execution,
            capability,
            args,
            deadline,
        } = envelope.frame
        else {
            return Frame::Error {
                request_id: None,
                code: "unexpected_frame".into(),
                message: "body accepts invoke frames from the controller".into(),
                operation_id: None,
            };
        };

        let actual = identity::current();
        if execution == ExecutionKind::System && actual.kind != ExecutionKind::System {
            let error_request_id = request_id.clone();
            let error_operation_id = operation_id.clone();
            return match system_router::invoke(
                &self.config,
                Envelope::new(
                    self.config.device_id.clone(),
                    envelope.seq,
                    Frame::Invoke {
                        request_id,
                        operation_id,
                        execution,
                        capability,
                        args,
                        deadline,
                    },
                ),
            )
            .await
            {
                Ok(frame) => frame,
                Err(error) => Frame::Error {
                    request_id: Some(error_request_id),
                    code: "system_router".into(),
                    message: error.to_string(),
                    operation_id: Some(error_operation_id),
                },
            };
        }
        if execution == ExecutionKind::Interactive && actual.kind != ExecutionKind::Interactive {
            let error_request_id = request_id.clone();
            let error_operation_id = operation_id.clone();
            return match interactive_router::invoke(
                &self.config,
                Envelope::new(
                    self.config.device_id.clone(),
                    envelope.seq,
                    Frame::Invoke {
                        request_id,
                        operation_id,
                        execution,
                        capability,
                        args,
                        deadline,
                    },
                ),
            )
            .await
            {
                Ok(frame) => frame,
                Err(error) => Frame::Error {
                    request_id: Some(error_request_id),
                    code: "interactive_router".into(),
                    message: error.to_string(),
                    operation_id: Some(error_operation_id),
                },
            };
        }
        if actual.kind != execution {
            return identity_unavailable(request_id, operation_id, execution, &actual);
        }
        let digest_capability = format!("{execution:?}:{capability}");
        let digest = match Journal::digest(&digest_capability, &args) {
            Ok(value) => value,
            Err(error) => {
                return error_frame(request_id, operation_id, "bad_args", error.to_string());
            }
        };
        if deadline.is_some_and(|value| value < Utc::now()) {
            return match self.journal.lookup(&request_id) {
                Ok(Some(record)) if record.args_digest == digest => {
                    self.handle_reused(request_id, record, actual).await
                }
                Ok(Some(_)) => error_frame(
                    request_id,
                    operation_id,
                    "id_conflict",
                    "request_id was already used with different execution arguments".into(),
                ),
                Ok(None) => error_frame(
                    request_id,
                    operation_id,
                    "deadline",
                    "operation deadline has already passed".into(),
                ),
                Err(error) => error_frame(request_id, operation_id, "journal", error.to_string()),
            };
        }
        match self
            .journal
            .admit(&request_id, &digest, &operation_id, &capability)
        {
            Ok(Admission::Conflict) => {
                return error_frame(
                    request_id,
                    operation_id,
                    "id_conflict",
                    "request_id was already used with different execution arguments".into(),
                );
            }
            Ok(Admission::Reused(record)) => {
                return self.handle_reused(request_id, record, actual).await;
            }
            Err(error) => {
                return error_frame(request_id, operation_id, "journal", error.to_string());
            }
            Ok(Admission::New) => {
                self.inflight
                    .lock()
                    .expect("inflight mutex poisoned")
                    .insert(request_id.clone());
            }
        }

        if capability == "process.start" {
            let process_args = match serde_json::from_value(args) {
                Ok(value) => value,
                Err(error) => {
                    return self.finish_error(
                        &request_id,
                        operation_id,
                        "bad_args",
                        error.to_string(),
                    );
                }
            };
            match process::start(&self.config.state_dir, &operation_id, process_args) {
                Ok(_) => {
                    let _ = self
                        .journal
                        .set_status(&request_id, OperationStatus::Starting);
                    self.clear_inflight(&request_id);
                    return Frame::Accepted {
                        request_id,
                        operation_id,
                        status: OperationStatus::Starting,
                        identity: actual,
                    };
                }
                Err(error) => {
                    // Та же причина, что ниже у `capability`: цепочка `.context(...)`
                    // из `process::start` иначе схлопывается в одну верхнюю строку.
                    return self.finish_error(
                        &request_id,
                        operation_id,
                        "process_start",
                        format!("{error:#}"),
                    );
                }
            }
        }

        match self.dispatch(&capability, args).await {
            Ok(result) => {
                let _ = self
                    .journal
                    .finish(&request_id, OperationStatus::Succeeded, &result);
                self.clear_inflight(&request_id);
                let artifacts = artifacts_from_result(&result);
                Frame::Result {
                    request_id,
                    operation_id,
                    status: OperationStatus::Succeeded,
                    ok: true,
                    result,
                    artifacts,
                }
            }
            Err(error) => {
                // ⚠ `{}` у anyhow печатает ТОЛЬКО верхний слой. Пока здесь стояло
                // `to_string()`, опечатка в аргументе читалки окна доезжала до неё как
                // «desktop.window.read arguments» — без слов «unknown field
                // `titel_contains`», то есть с потерянной причиной. Причина ошибки — это
                // тоже правда о пределах, и терять её нельзя: `{:#}` печатает цепочку.
                self.finish_error(
                    &request_id,
                    operation_id,
                    "capability",
                    format!("{error:#}"),
                )
            }
        }
    }

    async fn handle_reused(
        &self,
        request_id: String,
        record: RequestRecord,
        actual: ExecutionIdentity,
    ) -> Frame {
        if record.capability == "process.start" {
            match process::status(&self.config.state_dir, &record.operation_id, default_tail()) {
                Ok(result) => {
                    let status = result
                        .get("status")
                        .cloned()
                        .and_then(|value| serde_json::from_value(value).ok())
                        .unwrap_or(record.status);
                    if status.terminal() {
                        let _ = self.journal.finish(&request_id, status, &result);
                        let artifacts = artifacts_from_result(&result);
                        return Frame::Result {
                            request_id,
                            operation_id: record.operation_id,
                            status,
                            ok: status == OperationStatus::Succeeded,
                            result,
                            artifacts,
                        };
                    }
                    let _ = self.journal.set_status(&request_id, status);
                    return live_process_replay(request_id, record.operation_id, status, actual);
                }
                Err(_error) if record.result.is_none() => {
                    // A transient status read failure is not evidence that the detached
                    // supervisor is gone. Keep the controller response nonterminal too,
                    // otherwise the bridge would durably cache a false terminal Error.
                    return live_process_replay(
                        request_id,
                        record.operation_id,
                        record.status,
                        actual,
                    );
                }
                Err(_) => {}
            }
        }
        if let Some(result) = record.result {
            let artifacts = artifacts_from_result(&result);
            return Frame::Result {
                request_id,
                operation_id: record.operation_id,
                status: record.status,
                ok: record.status == OperationStatus::Succeeded,
                result,
                artifacts,
            };
        }
        // A duplicate deadline cannot cancel or summarize the original synchronous executor.
        // Stay attached until its durable terminal result is available; otherwise transport
        // would Ack the sole replayed Invoke and strand a result produced on the old socket.
        self.wait_for_reused(request_id, record.operation_id).await
    }

    async fn dispatch(&self, capability: &str, args: Value) -> Result<Value> {
        match route(capability) {
            Route::BodyStatus => Ok(json!({
                "ok": true,
                "identity": identity::current(),
                "manifest": self.manifest(),
            })),
            Route::Fs => fsops::dispatch(capability, args),
            Route::Artifact => self.artifacts.dispatch(capability, args).await,
            Route::ProcessStatus => {
                let args: OperationArgs = serde_json::from_value(args)?;
                process::status(&self.config.state_dir, &args.operation_id, args.tail)
            }
            Route::ProcessCancel => {
                let args: OperationArgs = serde_json::from_value(args)?;
                process::cancel(&self.config.state_dir, &args.operation_id)
            }
            Route::ProcessList => {
                let args: ProcessListArgs = serde_json::from_value(args)?;
                process::list(&self.config.state_dir, args.root.as_deref())
            }
            // `process.start` уезжает в отдельную ветку ещё в `handle` (у него своя
            // журнальная судьба). Сюда он попасть не должен — но и промолчать про него
            // нельзя: иначе имя, объявленное в манифесте, не имело бы маршрута вовсе.
            Route::ProcessStart => {
                anyhow::bail!("process.start is admitted before dispatch and must not reach it")
            }
            Route::WindowRead => uia::dispatch(capability, args),
            Route::Composed => compose::dispatch(capability, args, &self.config.state_dir).await,
            Route::Desktop => {
                let mut result = desktop::dispatch(capability, args, &self.config.state_dir)?;
                if let Some(output) = desktop::artifact_output(capability, &result)? {
                    let artifact = self
                        .artifacts
                        .upload(&output.path, output.name.as_deref())
                        .await?;
                    let object = result
                        .as_object_mut()
                        .context("desktop capability result must be an object")?;
                    object.insert("artifact".into(), serde_json::to_value(artifact)?);
                    object.insert(
                        "presentation".into(),
                        json!({
                            "kind": output.presentation,
                            "media_type": output.media_type,
                        }),
                    );
                }
                Ok(result)
            }
            Route::AdapterList => Ok(json!({"ok": true, "adapters": self.manifest().adapters})),
            Route::Unknown => anyhow::bail!("unknown body capability {capability}"),
        }
    }

    fn finish_error(
        &self,
        request_id: &str,
        operation_id: String,
        code: &str,
        message: String,
    ) -> Frame {
        let result = json!({"ok": false, "code": code, "error": message});
        let _ = self
            .journal
            .finish(request_id, OperationStatus::Failed, &result);
        self.clear_inflight(request_id);
        Frame::Error {
            request_id: Some(request_id.into()),
            code: code.into(),
            message: result["error"]
                .as_str()
                .unwrap_or("operation failed")
                .into(),
            operation_id: Some(operation_id),
        }
    }

    fn clear_inflight(&self, request_id: &str) {
        self.inflight
            .lock()
            .expect("inflight mutex poisoned")
            .remove(request_id);
    }

    async fn wait_for_reused(&self, request_id: String, operation_id: String) -> Frame {
        loop {
            match self.journal.lookup(&request_id) {
                Ok(Some(record)) => {
                    if let Some(result) = record.result {
                        let artifacts = artifacts_from_result(&result);
                        return Frame::Result {
                            request_id,
                            operation_id: record.operation_id,
                            status: record.status,
                            ok: record.status == OperationStatus::Succeeded,
                            result,
                            artifacts,
                        };
                    }
                }
                Ok(None) => {
                    return error_frame(
                        request_id,
                        operation_id,
                        "journal",
                        "replayed request disappeared from the durable journal".into(),
                    );
                }
                Err(error) => {
                    return error_frame(request_id, operation_id, "journal", error.to_string());
                }
            }
            let active = self
                .inflight
                .lock()
                .expect("inflight mutex poisoned")
                .contains(&request_id);
            if !active {
                // Synchronous capabilities execute inside this Runtime. If no executor is
                // registered and no terminal journal result exists, the executor was lost
                // (normally because the body process restarted mid-operation).
                let result = json!({
                    "ok": false,
                    "code": "operation_in_doubt",
                    "error": "the original in-process executor was lost before it left a terminal result",
                });
                let _ = self
                    .journal
                    .finish(&request_id, OperationStatus::InDoubt, &result);
                return Frame::Result {
                    request_id,
                    operation_id,
                    status: OperationStatus::InDoubt,
                    ok: false,
                    result,
                    artifacts: Vec::new(),
                };
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
    }
}

fn artifacts_from_result(result: &Value) -> Vec<ArtifactRef> {
    result
        .get("artifact")
        .cloned()
        .and_then(|value| serde_json::from_value(value).ok())
        .into_iter()
        .collect()
}

fn live_process_replay(
    request_id: String,
    operation_id: String,
    status: OperationStatus,
    identity: ExecutionIdentity,
) -> Frame {
    match status {
        OperationStatus::Running | OperationStatus::Cancelling => Frame::Progress {
            request_id,
            operation_id,
            status,
            log_offset: 0,
            preview: Some("detached process is still active".into()),
        },
        _ => Frame::Accepted {
            request_id,
            operation_id,
            status,
            identity,
        },
    }
}

fn identity_unavailable(
    request_id: String,
    operation_id: String,
    requested: ExecutionKind,
    actual: &ExecutionIdentity,
) -> Frame {
    error_frame(
        request_id,
        operation_id,
        "identity_unavailable",
        format!(
            "requested {requested:?}, current body is {:?} with {:?} integrity",
            actual.kind, actual.integrity
        ),
    )
}

fn error_frame(request_id: String, operation_id: String, code: &str, message: String) -> Frame {
    Frame::Error {
        request_id: Some(request_id),
        code: code.into(),
        message,
        operation_id: Some(operation_id),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// ⚠ Ветка читалки окна ОБЯЗАНА стоять выше общей `desktop.`: имя начинается с того же
    /// префикса. Если порядок когда-нибудь перепутают, глагол не исчезнет из манифеста —
    /// он начнёт отвечать «такого глагола нет», что хуже: объявлено и неисполнимо.
    #[test]
    fn window_read_and_composed_steps_are_routed_before_the_desktop_prefix() {
        assert_eq!(route(uia::CAPABILITY), Route::WindowRead);
        assert_eq!(route(compose::ACT_AND_READ), Route::Composed);
        assert_eq!(route(compose::WAIT), Route::Composed);
        // Соседи по префиксу не сдвинулись.
        assert_eq!(route("desktop.input.perform"), Route::Desktop);
        assert_eq!(route("desktop.screen.capture"), Route::Desktop);
        assert_eq!(route("os.process.list"), Route::Desktop);
        // И файловые имена по-прежнему делятся на артефактные и обычные.
        assert_eq!(route("fs.export"), Route::Artifact);
        assert_eq!(route("fs.import"), Route::Artifact);
        assert_eq!(route("fs.delete"), Route::Fs);
        assert_eq!(route("fs.stat"), Route::Fs);
        assert_eq!(route("desktop.window.dance"), Route::Desktop);
        assert_eq!(route("nothing.at.all"), Route::Unknown);
    }

    /// Закон «объявленное исполняется». Именно здесь умерли бы `office.com` и `win32.api`,
    /// которые годами стояли в манифесте, не имея ни одного обработчика.
    #[test]
    fn every_declared_capability_has_somewhere_to_go() {
        let manifest = identity::manifest("test-device");
        for descriptor in &manifest.capabilities {
            assert_ne!(
                route(&descriptor.name),
                Route::Unknown,
                "manifest declares {} and nothing dispatches it",
                descriptor.name
            );
        }
        for adapter in &manifest.adapters {
            for name in &adapter.capabilities {
                assert_ne!(
                    route(name),
                    Route::Unknown,
                    "adapter {} declares {name} and nothing dispatches it",
                    adapter.name
                );
                assert!(
                    manifest
                        .capabilities
                        .iter()
                        .any(|descriptor| &descriptor.name == name),
                    "adapter {} declares {name}, which is not in the capability list",
                    adapter.name
                );
            }
        }
    }

    /// Обратная сторона того же закона: исполнимое объявляется. Новые имена проверяем
    /// поимённо, а файловые — живой пробой: `fsops` отвечает «unknown filesystem
    /// capability» ровно на то, чего у него нет, и пустые аргументы до диска не доходят.
    #[test]
    fn every_new_verb_is_declared_and_answers_to_its_name() {
        let manifest = identity::manifest("test-device");
        let declared: Vec<&str> = manifest
            .capabilities
            .iter()
            .map(|descriptor| descriptor.name.as_str())
            .collect();
        for name in [
            uia::CAPABILITY,
            compose::ACT_AND_READ,
            compose::WAIT,
            "fs.delete",
            "fs.move",
            "fs.copy",
            "fs.mkdir",
        ] {
            assert!(
                declared.contains(&name),
                "{name} is missing from the manifest"
            );
        }
        for name in declared {
            if !name.starts_with("fs.") || name == "fs.export" || name == "fs.import" {
                continue;
            }
            let error = fsops::dispatch(name, json!({}))
                .expect_err("empty arguments cannot be a valid filesystem call")
                .to_string();
            assert!(
                !error.contains("unknown filesystem capability"),
                "{name} is declared but fsops does not implement it"
            );
        }
    }

    /// Ошибка обязана доехать до неё целиком. Проба берёт заведомо неверный аргумент
    /// читалки окна: разбор падает ДО всякого COM, а сообщение состоит из двух слоёв —
    /// «чьи это аргументы» и «какое поле не то».
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn a_capability_error_keeps_its_cause_on_the_way_to_her() {
        let (runtime, root) = test_runtime("praxis-runtime-error-chain");
        let execution = identity::current().kind;
        let frame = runtime
            .handle(Envelope::new(
                "test-device",
                1,
                Frame::Invoke {
                    request_id: "chain-request".into(),
                    operation_id: "chain-operation".into(),
                    execution,
                    capability: uia::CAPABILITY.into(),
                    args: json!({"titel_contains": "Блокнот"}),
                    deadline: None,
                },
            ))
            .await;
        let Frame::Error { message, .. } = frame else {
            panic!("expected an error frame");
        };
        assert!(
            message.contains("desktop.window.read arguments"),
            "{message}"
        );
        assert!(message.contains("titel_contains"), "{message}");

        drop(runtime);
        let _ = std::fs::remove_dir_all(root);
    }

    fn test_runtime(prefix: &str) -> (Arc<Runtime>, std::path::PathBuf) {
        let root = std::env::temp_dir().join(format!("{prefix}-{}", uuid::Uuid::new_v4()));
        let config = BodyConfig {
            device_id: "test-device".into(),
            bridge_ws_url: "ws://127.0.0.1:1".into(),
            artifact_base_url: "http://127.0.0.1:1".into(),
            dial_addresses: Vec::new(),
            token: "test-token".into(),
            state_dir: root.clone(),
            artifact_chunk_size: 4096,
            system_router_pipe: "unused-system".into(),
            system_router_token: String::new(),
            interactive_router_pipe: "unused-interactive".into(),
            interactive_router_token: String::new(),
            interactive_user_sid: String::new(),
        };
        (Arc::new(Runtime::open(config).unwrap()), root)
    }

    #[tokio::test]
    async fn replay_waits_for_the_original_terminal_result() {
        let (runtime, root) = test_runtime("praxis-runtime-wait");
        let request_id = "replayed-request";
        let operation_id = "replayed-operation";
        assert!(matches!(
            runtime
                .journal
                .admit(request_id, "digest", operation_id, "fs.stat")
                .unwrap(),
            Admission::New
        ));
        runtime.inflight.lock().unwrap().insert(request_id.into());

        let finisher = runtime.clone();
        tokio::spawn(async move {
            tokio::time::sleep(Duration::from_millis(75)).await;
            finisher
                .journal
                .finish(
                    request_id,
                    OperationStatus::Succeeded,
                    &json!({"ok": true, "value": 42}),
                )
                .unwrap();
            finisher.clear_inflight(request_id);
        });

        let frame = runtime
            .wait_for_reused(request_id.into(), operation_id.into())
            .await;
        assert!(matches!(
            frame,
            Frame::Result {
                status: OperationStatus::Succeeded,
                ok: true,
                ..
            }
        ));
        drop(runtime);
        let _ = std::fs::remove_dir_all(root);
    }

    #[tokio::test]
    async fn expired_active_replay_waits_for_the_durable_terminal_result() {
        let (runtime, root) = test_runtime("praxis-runtime-deadline");
        let request_id = "deadline-request";
        let operation_id = "deadline-operation";
        let execution = identity::current().kind;
        let args = json!({"path": "ignored"});
        let digest = Journal::digest(&format!("{execution:?}:fs.stat"), &args).unwrap();
        assert!(matches!(
            runtime
                .journal
                .admit(request_id, &digest, operation_id, "fs.stat")
                .unwrap(),
            Admission::New
        ));
        runtime.inflight.lock().unwrap().insert(request_id.into());

        let finisher = runtime.clone();
        tokio::spawn(async move {
            tokio::time::sleep(Duration::from_millis(75)).await;
            finisher
                .journal
                .finish(
                    request_id,
                    OperationStatus::Succeeded,
                    &json!({"ok": true, "value": 42}),
                )
                .unwrap();
            finisher.clear_inflight(request_id);
        });
        let frame = runtime
            .handle(Envelope::new(
                "test-device",
                1,
                Frame::Invoke {
                    request_id: request_id.into(),
                    operation_id: operation_id.into(),
                    execution,
                    capability: "fs.stat".into(),
                    args,
                    deadline: Some(Utc::now() - chrono::Duration::seconds(1)),
                },
            ))
            .await;
        assert!(matches!(
            frame,
            Frame::Result {
                status: OperationStatus::Succeeded,
                ok: true,
                ..
            }
        ));
        let record = runtime.journal.lookup(request_id).unwrap().unwrap();
        assert_eq!(record.status, OperationStatus::Succeeded);
        assert!(record.result.is_some());

        drop(runtime);
        let _ = std::fs::remove_dir_all(root);
    }

    #[tokio::test]
    async fn expired_replay_returns_the_saved_result_but_new_work_is_rejected() {
        let (runtime, root) = test_runtime("praxis-runtime-expired-replay");
        let execution = identity::current().kind;
        let capability = "fs.export";
        let args = json!({"path": "ignored"});
        let digest = Journal::digest(&format!("{execution:?}:{capability}"), &args).unwrap();
        let request_id = "expired-saved-request";
        let operation_id = "expired-saved-operation";
        let artifact = ArtifactRef {
            sha256: "a".repeat(64),
            size: 42,
            name: "saved.txt".into(),
            mime: Some("text/plain".into()),
            source_device: Some("test-device".into()),
        };
        assert!(matches!(
            runtime
                .journal
                .admit(request_id, &digest, operation_id, capability)
                .unwrap(),
            Admission::New
        ));
        runtime
            .journal
            .finish(
                request_id,
                OperationStatus::Succeeded,
                &json!({"ok": true, "value": 42, "artifact": artifact}),
            )
            .unwrap();

        let saved = runtime
            .handle(Envelope::new(
                "test-device",
                1,
                Frame::Invoke {
                    request_id: request_id.into(),
                    operation_id: operation_id.into(),
                    execution,
                    capability: capability.into(),
                    args: args.clone(),
                    deadline: Some(Utc::now() - chrono::Duration::seconds(1)),
                },
            ))
            .await;
        let Frame::Result {
            status,
            ok,
            artifacts,
            ..
        } = saved
        else {
            panic!("expected saved result");
        };
        assert_eq!(status, OperationStatus::Succeeded);
        assert!(ok);
        assert_eq!(artifacts.len(), 1);
        assert_eq!(artifacts[0].name, "saved.txt");

        let new_request_id = "expired-new-request";
        let rejected = runtime
            .handle(Envelope::new(
                "test-device",
                2,
                Frame::Invoke {
                    request_id: new_request_id.into(),
                    operation_id: "expired-new-operation".into(),
                    execution,
                    capability: capability.into(),
                    args,
                    deadline: Some(Utc::now() - chrono::Duration::seconds(1)),
                },
            ))
            .await;
        assert!(matches!(
            rejected,
            Frame::Error { ref code, .. } if code == "deadline"
        ));
        assert!(runtime.journal.lookup(new_request_id).unwrap().is_none());

        drop(runtime);
        let _ = std::fs::remove_dir_all(root);
    }

    #[tokio::test]
    async fn missing_in_process_executor_is_durably_in_doubt() {
        let (runtime, root) = test_runtime("praxis-runtime-lost");
        let request_id = "lost-request";
        let operation_id = "lost-operation";
        assert!(matches!(
            runtime
                .journal
                .admit(request_id, "digest", operation_id, "fs.stat")
                .unwrap(),
            Admission::New
        ));

        let frame = runtime
            .wait_for_reused(request_id.into(), operation_id.into())
            .await;
        assert!(matches!(
            frame,
            Frame::Result {
                status: OperationStatus::InDoubt,
                ok: false,
                ..
            }
        ));
        let record = runtime.journal.lookup(request_id).unwrap().unwrap();
        assert_eq!(record.status, OperationStatus::InDoubt);
        assert!(record.result.is_some());

        drop(runtime);
        let _ = std::fs::remove_dir_all(root);
    }

    #[tokio::test]
    async fn replay_of_a_live_detached_process_stays_nonterminal() {
        let (runtime, root) = test_runtime("praxis-runtime-process-replay");
        let request_id = "live-process-request";
        let operation_id = "live-process-operation";
        let execution = identity::current().kind;
        let args = json!({"program": "unused-test-program"});
        let digest = Journal::digest(&format!("{execution:?}:process.start"), &args).unwrap();
        assert!(matches!(
            runtime
                .journal
                .admit(request_id, &digest, operation_id, "process.start")
                .unwrap(),
            Admission::New
        ));

        let operation_dir = root.join("operations").join(operation_id);
        std::fs::create_dir_all(&operation_dir).unwrap();
        let now = Utc::now();
        std::fs::write(
            operation_dir.join("state.json"),
            serde_json::to_vec(&json!({
                "operation_id": operation_id,
                "status": OperationStatus::Running,
                "supervisor_pid": std::process::id(),
                "child_pid": null,
                "identity": identity::current(),
                "created_at": now,
                "updated_at": now,
                "started_at": null,
                "finished_at": null,
            }))
            .unwrap(),
        )
        .unwrap();

        let frame = runtime
            .handle(Envelope::new(
                runtime.config.device_id.clone(),
                1,
                Frame::Invoke {
                    request_id: request_id.into(),
                    operation_id: operation_id.into(),
                    execution,
                    capability: "process.start".into(),
                    args,
                    deadline: None,
                },
            ))
            .await;
        assert!(matches!(
            frame,
            Frame::Progress {
                status: OperationStatus::Running,
                ..
            }
        ));
        let record = runtime.journal.lookup(request_id).unwrap().unwrap();
        assert_eq!(record.status, OperationStatus::Running);
        assert!(record.result.is_none());

        drop(runtime);
        let _ = std::fs::remove_dir_all(root);
    }
}
