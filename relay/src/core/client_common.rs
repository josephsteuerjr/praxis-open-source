/// The `instructions` field in the payload sent to a model should always start
/// with this content.
pub const BASE_INSTRUCTIONS: &str = include_str!("prompt.md");