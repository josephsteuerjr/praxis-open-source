use anyhow::Result;
use praxis_body_protocol::{Envelope, Frame};

use crate::config::BodyConfig;
use crate::local_router;

pub fn available(config: &BodyConfig) -> bool {
    local_router::available(
        &config.interactive_router_pipe,
        &config.interactive_user_sid,
    )
}

pub async fn invoke(config: &BodyConfig, envelope: Envelope) -> Result<Frame> {
    local_router::invoke(
        config.interactive_router_pipe.clone(),
        config.interactive_user_sid.clone(),
        config.interactive_router_token.clone(),
        envelope,
    )
    .await
}
