//! Wire types shared by `praxis-body`, `praxis-bridge`, and the server-side
//! Forge adapter. Nothing in this crate decides what Praxis should do.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use uuid::Uuid;

pub const PROTOCOL: &str = "praxis.body.v1";

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PeerRole {
    Device,
    Controller,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ExecutionKind {
    Interactive,
    System,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum IntegrityLevel {
    Untrusted,
    Low,
    Medium,
    High,
    System,
    Protected,
    Unknown,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ExecutionIdentity {
    pub kind: ExecutionKind,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub user_sid: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub session_id: Option<u32>,
    pub integrity: IntegrityLevel,
    pub elevated: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ExecutionContextCapability {
    pub kind: ExecutionKind,
    pub available: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub identity: Option<ExecutionIdentity>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub unavailable_reason: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CapabilityDescriptor {
    pub name: String,
    pub version: u32,
    pub mutating: bool,
    pub durable: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AdapterDescriptor {
    pub name: String,
    pub version: String,
    pub capabilities: Vec<String>,
    pub available: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CapabilityManifest {
    pub protocol: String,
    pub body_version: String,
    pub device_id: String,
    pub hostname: String,
    pub os: String,
    pub arch: String,
    pub execution_contexts: Vec<ExecutionContextCapability>,
    pub capabilities: Vec<CapabilityDescriptor>,
    #[serde(default)]
    pub adapters: Vec<AdapterDescriptor>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ArtifactRef {
    pub sha256: String,
    pub size: u64,
    pub name: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub mime: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source_device: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum OperationStatus {
    Admitted,
    Starting,
    Running,
    Cancelling,
    Succeeded,
    Failed,
    Cancelled,
    TimedOut,
    InDoubt,
}

impl OperationStatus {
    pub fn terminal(self) -> bool {
        matches!(
            self,
            Self::Succeeded | Self::Failed | Self::Cancelled | Self::TimedOut | Self::InDoubt
        )
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Envelope {
    pub protocol: String,
    pub message_id: Uuid,
    pub device_id: String,
    pub seq: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub ack: Option<u64>,
    #[serde(flatten)]
    pub frame: Frame,
}

impl Envelope {
    pub fn new(device_id: impl Into<String>, seq: u64, frame: Frame) -> Self {
        Self {
            protocol: PROTOCOL.to_string(),
            message_id: Uuid::new_v4(),
            device_id: device_id.into(),
            seq,
            ack: None,
            frame,
        }
    }

    pub fn validate(&self) -> Result<(), String> {
        if self.protocol != PROTOCOL {
            return Err(format!("need {PROTOCOL}, got {}", self.protocol));
        }
        if self.device_id.trim().is_empty() {
            return Err("device_id is empty".to_string());
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum Frame {
    Hello {
        role: PeerRole,
        instance_id: Uuid,
        resume_from: u64,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        capabilities: Option<CapabilityManifest>,
    },
    HelloAck {
        bridge_instance_id: Uuid,
        resume_from: u64,
        connected_at: DateTime<Utc>,
    },
    Heartbeat {
        at: DateTime<Utc>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        active_operations: Option<u32>,
    },
    Ack {
        through_seq: u64,
    },
    Invoke {
        request_id: String,
        operation_id: String,
        execution: ExecutionKind,
        capability: String,
        #[serde(default)]
        args: Value,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        deadline: Option<DateTime<Utc>>,
    },
    Accepted {
        request_id: String,
        operation_id: String,
        status: OperationStatus,
        identity: ExecutionIdentity,
    },
    Progress {
        request_id: String,
        operation_id: String,
        status: OperationStatus,
        #[serde(default)]
        log_offset: u64,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        preview: Option<String>,
    },
    Result {
        request_id: String,
        operation_id: String,
        status: OperationStatus,
        ok: bool,
        #[serde(default)]
        result: Value,
        #[serde(default)]
        artifacts: Vec<ArtifactRef>,
    },
    Error {
        #[serde(default, skip_serializing_if = "Option::is_none")]
        request_id: Option<String>,
        code: String,
        message: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        operation_id: Option<String>,
    },
    Cancel {
        request_id: String,
        operation_id: String,
    },
    Cancelled {
        request_id: String,
        operation_id: String,
        status: OperationStatus,
    },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ArtifactStatus {
    pub artifact: ArtifactRef,
    pub complete: bool,
    #[serde(default)]
    pub received_offsets: Vec<u64>,
    pub chunk_size: u64,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn system_execution_round_trips_without_becoming_interactive() {
        let env = Envelope::new(
            "windows-pc",
            7,
            Frame::Invoke {
                request_id: "req-1".into(),
                operation_id: "op-1".into(),
                execution: ExecutionKind::System,
                capability: "process.start".into(),
                args: serde_json::json!({"program": "whoami.exe"}),
                deadline: None,
            },
        );
        let raw = serde_json::to_string(&env).unwrap();
        let decoded: Envelope = serde_json::from_str(&raw).unwrap();
        assert_eq!(decoded, env);
        assert!(matches!(
            decoded.frame,
            Frame::Invoke {
                execution: ExecutionKind::System,
                ..
            }
        ));
    }

    #[test]
    fn protocol_mismatch_is_explicit() {
        let mut env = Envelope::new(
            "windows-pc",
            1,
            Frame::Heartbeat {
                at: Utc::now(),
                active_operations: Some(0),
            },
        );
        env.protocol = "praxis.body.v0".into();
        assert!(env.validate().unwrap_err().contains(PROTOCOL));
    }

    #[test]
    fn terminal_status_is_not_guessed_from_strings() {
        assert!(!OperationStatus::Running.terminal());
        assert!(OperationStatus::Succeeded.terminal());
        assert!(OperationStatus::InDoubt.terminal());
    }
}
