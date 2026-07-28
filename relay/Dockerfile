# syntax=docker/dockerfile:1
# Multi-stage build. Binary hardcodes bind to 127.0.0.1:5011 -> run with network_mode: host.
# Runtime binary is renamed to a neutral name so the process doesn't advertise its purpose.
FROM rust:1-bookworm AS builder
ENV CARGO_TERM_COLOR=always
ENV CARGO_BUILD_JOBS=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    pkg-config libssl-dev ca-certificates cmake perl build-essential \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /usr/src/codex
COPY Cargo.toml Cargo.lock ./
RUN mkdir -p src && printf 'fn main(){}\n' > src/main.rs \
    && cargo build --release || true
COPY src ./src
RUN touch src/main.rs && cargo build --release

FROM debian:bookworm-slim AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
    libssl3 ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
# Rename on copy -> process name is neutral.
COPY --from=builder /usr/src/codex/target/release/codex-proxy-server /usr/local/bin/relay
ENV HOME=/root RUST_LOG=info RELAY_LOG_DIR=/app/logs
EXPOSE 5011
# main() shows an interactive menu; feed "1" (Run server) on stdin to auto-start.
CMD ["sh", "-c", "echo 1 | relay"]
