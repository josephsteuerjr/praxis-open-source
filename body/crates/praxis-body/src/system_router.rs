use anyhow::Result;
use praxis_body_protocol::{Envelope, Frame};

use crate::config::BodyConfig;
use crate::local_router;

pub fn available(config: &BodyConfig) -> bool {
    local_router::available(&config.system_router_pipe, "S-1-5-18")
}

pub async fn invoke(config: &BodyConfig, envelope: Envelope) -> Result<Frame> {
    local_router::invoke(
        config.system_router_pipe.clone(),
        "S-1-5-18".into(),
        config.system_router_token.clone(),
        envelope,
    )
    .await
}
