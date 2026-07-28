use std::collections::HashSet;
use std::net::SocketAddr;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use anyhow::{Context, Result};
use chrono::Utc;
use futures_util::{SinkExt, StreamExt};
use http::header::{AUTHORIZATION, HeaderValue};
use praxis_body_protocol::{Envelope, Frame, PeerRole};
use tokio::net::{TcpStream, lookup_host};
use tokio::sync::mpsc;
use tokio::time::Instant;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::{MaybeTlsStream, WebSocketStream, client_async_tls};
use tracing::{info, warn};
use url::Url;
use uuid::Uuid;

use crate::runtime::Runtime;

const DIAL_ATTEMPT_TIMEOUT: Duration = Duration::from_secs(5);
const DNS_TIMEOUT: Duration = Duration::from_secs(10);
const MAX_DNS_ADDRESSES: usize = 8;
const HELLO_TIMEOUT: Duration = Duration::from_secs(15);
const HEARTBEAT_INTERVAL: Duration = Duration::from_secs(20);
const PEER_DEAD_AFTER: Duration = Duration::from_secs(60);
const REQUEST_QUEUE: usize = 64;
const OUTBOUND_QUEUE: usize = 64;

struct WorkItem {
    envelope: Envelope,
    inbound_seq: u64,
}

type BridgeSocket = WebSocketStream<MaybeTlsStream<TcpStream>>;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum DialSource {
    Configured,
    Dns,
}

impl DialSource {
    fn as_str(self) -> &'static str {
        match self {
            Self::Configured => "configured",
            Self::Dns => "dns",
        }
    }
}

fn bridge_request(url: &Url, token: &str) -> Result<http::Request<()>> {
    let mut request = url.as_str().into_client_request()?;
    request.headers_mut().insert(
        AUTHORIZATION,
        HeaderValue::from_str(&format!("Bearer {token}"))?,
    );
    Ok(request)
}

async fn dial_address(url: &Url, token: &str, address: SocketAddr) -> Result<BridgeSocket> {
    let request = bridge_request(url, token)?;
    let attempt = async move {
        let stream = TcpStream::connect(address)
            .await
            .with_context(|| format!("connect TCP {address}"))?;
        stream
            .set_nodelay(true)
            .with_context(|| format!("set TCP_NODELAY for {address}"))?;
        // `client_async_tls` derives TLS SNI and the websocket Host header from `request`.
        // Only the already-connected TCP destination is overridden.
        let (socket, _) = client_async_tls(request, stream)
            .await
            .with_context(|| format!("upgrade canonical websocket through {address}"))?;
        Ok::<_, anyhow::Error>(socket)
    };
    tokio::time::timeout(DIAL_ATTEMPT_TIMEOUT, attempt)
        .await
        .with_context(|| format!("bridge dial {address} timed out"))?
}

async fn canonical_dns_addresses(url: &Url) -> Result<Vec<SocketAddr>> {
    let host = url.host_str().context("bridge URL has no host")?;
    let port = url
        .port_or_known_default()
        .context("bridge URL has no known transport port")?;
    let resolved = tokio::time::timeout(DNS_TIMEOUT, lookup_host((host, port)))
        .await
        .context("bridge DNS resolution timed out")?
        .with_context(|| format!("resolve canonical bridge host {host}"))?;
    let mut unique = HashSet::new();
    Ok(resolved
        .filter(|address| unique.insert(*address))
        .take(MAX_DNS_ADDRESSES)
        .collect())
}

async fn dial_bridge(
    url: &Url,
    token: &str,
    configured: &[SocketAddr],
) -> Result<(BridgeSocket, SocketAddr, DialSource)> {
    let mut attempted = HashSet::new();
    let mut failures = Vec::new();

    for &address in configured {
        attempted.insert(address);
        match dial_address(url, token, address).await {
            Ok(socket) => return Ok((socket, address, DialSource::Configured)),
            Err(error) => failures.push(format!("configured {address}: {error:#}")),
        }
    }

    match canonical_dns_addresses(url).await {
        Ok(addresses) => {
            for address in addresses {
                if !attempted.insert(address) {
                    continue;
                }
                match dial_address(url, token, address).await {
                    Ok(socket) => return Ok((socket, address, DialSource::Dns)),
                    Err(error) => failures.push(format!("dns {address}: {error:#}")),
                }
            }
        }
        Err(error) => failures.push(format!("dns: {error:#}")),
    }

    if failures.is_empty() {
        anyhow::bail!("canonical bridge host resolved to no dialable addresses");
    }
    anyhow::bail!("all bridge dial routes failed: {}", failures.join("; "))
}

pub async fn connect_forever(runtime: Arc<Runtime>) -> Result<()> {
    let mut delay = Duration::from_secs(1);
    loop {
        let established = Arc::new(AtomicBool::new(false));
        match connect_once(runtime.clone(), established.clone()).await {
            Ok(()) => warn!(
                established = established.load(Ordering::Relaxed),
                "bridge connection closed"
            ),
            Err(error) => warn!(
                %error,
                established = established.load(Ordering::Relaxed),
                "bridge connection failed"
            ),
        }
        if established.load(Ordering::Relaxed) {
            delay = Duration::from_secs(1);
        }
        tokio::time::sleep(delay).await;
        if !established.load(Ordering::Relaxed) {
            delay = (delay * 2).min(Duration::from_secs(30));
        }
    }
}

async fn ordered_worker(
    runtime: Arc<Runtime>,
    mut requests: mpsc::Receiver<WorkItem>,
    outbound: mpsc::Sender<Message>,
) -> Result<()> {
    while let Some(item) = requests.recv().await {
        let response = runtime.handle(item.envelope).await;
        enqueue_response_then_ack(&runtime, &outbound, response, item.inbound_seq).await?;
    }
    Ok(())
}

async fn enqueue_response_then_ack(
    runtime: &Runtime,
    outbound: &mpsc::Sender<Message>,
    response: Frame,
    inbound_seq: u64,
) -> Result<()> {
    // The bridge's Ack removes the Invoke from its durable replay spool. Queue the response
    // first so a disconnect can never acknowledge work whose result was not handed to the
    // ordered socket writer yet.
    let response = outbound_message(runtime, response)?;
    outbound
        .send(response)
        .await
        .context("bridge writer closed before request response")?;
    let ack = outbound_message(
        runtime,
        Frame::Ack {
            through_seq: inbound_seq,
        },
    )?;
    outbound
        .send(ack)
        .await
        .context("bridge writer closed before request acknowledgement")?;
    Ok(())
}

fn outbound_message(runtime: &Runtime, frame: Frame) -> Result<Message> {
    let envelope = Envelope::new(
        runtime.config.device_id.clone(),
        runtime.journal().next_seq("outbound")?,
        frame,
    );
    Ok(Message::Text(serde_json::to_string(&envelope)?.into()))
}

async fn connect_once(runtime: Arc<Runtime>, established: Arc<AtomicBool>) -> Result<()> {
    let mut url = Url::parse(&runtime.config.bridge_ws_url)?;
    {
        let mut segments = url
            .path_segments_mut()
            .map_err(|_| anyhow::anyhow!("bridge URL cannot be a base"))?;
        segments.pop_if_empty();
        segments.extend(["v1", "ws", "device", &runtime.config.device_id]);
    }
    let (mut socket, dial_address, dial_source) =
        dial_bridge(&url, &runtime.config.token, &runtime.config.dial_addresses).await?;
    let hello = Envelope::new(
        runtime.config.device_id.clone(),
        runtime.journal().next_seq("outbound")?,
        Frame::Hello {
            role: PeerRole::Device,
            instance_id: Uuid::new_v4(),
            resume_from: 0,
            capabilities: Some(runtime.manifest()),
        },
    );
    socket
        .send(Message::Text(serde_json::to_string(&hello)?.into()))
        .await?;

    let (mut sink, mut stream) = socket.split();
    let (outbound_tx, mut outbound_rx) = mpsc::channel::<Message>(OUTBOUND_QUEUE);
    let mut writer = tokio::spawn(async move {
        while let Some(message) = outbound_rx.recv().await {
            sink.send(message).await?;
        }
        Ok::<(), anyhow::Error>(())
    });
    let (request_tx, request_rx) = mpsc::channel::<WorkItem>(REQUEST_QUEUE);
    let mut worker = tokio::spawn(ordered_worker(
        runtime.clone(),
        request_rx,
        outbound_tx.clone(),
    ));

    let hello_timeout = tokio::time::sleep(HELLO_TIMEOUT);
    tokio::pin!(hello_timeout);
    let mut hello_complete = false;
    let mut last_inbound = Instant::now();
    let mut heartbeat = tokio::time::interval(HEARTBEAT_INTERVAL);
    heartbeat.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);

    let result = loop {
        tokio::select! {
            _ = &mut hello_timeout, if !hello_complete => {
                break Err(anyhow::anyhow!("bridge HelloAck timed out"));
            }
            writer_result = &mut writer => {
                break match writer_result {
                    Ok(Ok(())) => Err(anyhow::anyhow!("bridge writer closed")),
                    Ok(Err(error)) => Err(error.context("bridge writer failed")),
                    Err(error) => Err(anyhow::Error::new(error).context("bridge writer task failed")),
                };
            }
            worker_result = &mut worker => {
                break match worker_result {
                    Ok(Ok(())) => Err(anyhow::anyhow!("bridge request worker closed")),
                    Ok(Err(error)) => Err(error.context("bridge request worker failed")),
                    Err(error) => Err(anyhow::Error::new(error).context("bridge request worker task failed")),
                };
            }
            _ = heartbeat.tick() => {
                if hello_complete && last_inbound.elapsed() >= PEER_DEAD_AFTER {
                    break Err(anyhow::anyhow!("bridge peer stopped responding"));
                }
                let heartbeat = match outbound_message(
                    &runtime,
                    Frame::Heartbeat { at: Utc::now(), active_operations: None },
                ) {
                    Ok(message) => message,
                    Err(error) => break Err(error),
                };
                if outbound_tx.try_send(heartbeat).is_err()
                    || outbound_tx.try_send(Message::Ping(Vec::new().into())).is_err()
                {
                    break Err(anyhow::anyhow!("bridge outbound queue is unavailable"));
                }
            }
            message = stream.next() => {
                let Some(message) = message else { break Ok(()); };
                let message = match message {
                    Ok(message) => message,
                    Err(error) => break Err(error.into()),
                };
                last_inbound = Instant::now();
                match message {
                    Message::Text(raw) => {
                        let envelope: Envelope = match serde_json::from_str(raw.as_str()) {
                            Ok(envelope) => envelope,
                            Err(error) => break Err(error.into()),
                        };
                        if let Err(error) = envelope.validate() {
                            break Err(anyhow::anyhow!(error));
                        }
                        if envelope.device_id != runtime.config.device_id {
                            warn!(frame_device = %envelope.device_id, "ignored frame for another device");
                            continue;
                        }
                        if matches!(envelope.frame, Frame::HelloAck { .. }) {
                            if !hello_complete {
                                hello_complete = true;
                                established.store(true, Ordering::Relaxed);
                                info!(
                                    url = %url,
                                    %dial_address,
                                    dial_source = dial_source.as_str(),
                                    "body connected to bridge"
                                );
                            }
                            continue;
                        }
                        let inbound_seq = envelope.seq;
                        // A replay burst is ordinary load, not a connection fault. Apply bounded
                        // backpressure here; the websocket reader resumes as the ordered worker
                        // drains the queue, preserving sequence and avoiding reconnect thrash.
                        if request_tx.send(WorkItem { envelope, inbound_seq }).await.is_err() {
                            break Err(anyhow::anyhow!("bridge request worker is unavailable"));
                        }
                    }
                    Message::Ping(value) => {
                        if outbound_tx.try_send(Message::Pong(value)).is_err() {
                            break Err(anyhow::anyhow!("bridge outbound queue is unavailable"));
                        }
                    }
                    Message::Pong(_) => {}
                    Message::Close(_) => break Ok(()),
                    _ => {}
                }
            }
        }
    };

    drop(request_tx);
    drop(outbound_tx);
    writer.abort();
    // Do not abort an in-flight durable operation. It may finish the local journal even though
    // this socket is gone; replay on the next connection will then return its exact result.
    drop(worker);
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    use praxis_body_protocol::OperationStatus;
    use tokio::net::TcpListener;
    use tokio::sync::oneshot;
    use tokio_tungstenite::accept_hdr_async;
    use tokio_tungstenite::tungstenite::handshake::server::{Request, Response};

    use crate::config::BodyConfig;

    fn frame(message: Message) -> Envelope {
        let Message::Text(raw) = message else {
            panic!("expected text envelope")
        };
        serde_json::from_str(raw.as_str()).unwrap()
    }

    async fn websocket_probe() -> (
        SocketAddr,
        oneshot::Receiver<(String, String, String)>,
        tokio::task::JoinHandle<()>,
    ) {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let (tx, rx) = oneshot::channel();
        let task = tokio::spawn(async move {
            let (stream, _) = listener.accept().await.unwrap();
            let mut tx = Some(tx);
            let _socket = accept_hdr_async(stream, move |request: &Request, response: Response| {
                let host = request
                    .headers()
                    .get("host")
                    .and_then(|value| value.to_str().ok())
                    .unwrap_or_default()
                    .to_string();
                let authorization = request
                    .headers()
                    .get(AUTHORIZATION)
                    .and_then(|value| value.to_str().ok())
                    .unwrap_or_default()
                    .to_string();
                let _ = tx
                    .take()
                    .unwrap()
                    .send((request.uri().to_string(), host, authorization));
                Ok(response)
            })
            .await
            .unwrap();
        });
        (address, rx, task)
    }

    #[tokio::test]
    async fn configured_dial_keeps_canonical_websocket_identity() {
        let (address, observed, server) = websocket_probe().await;
        let url = Url::parse(&format!(
            "ws://body.example.invalid:{}/body/v1/ws/device/test-device",
            address.port()
        ))
        .unwrap();

        let (socket, actual, source) = dial_bridge(&url, "device-token", &[address]).await.unwrap();
        assert_eq!(actual, address);
        assert_eq!(source, DialSource::Configured);
        let (uri, host, authorization) = observed.await.unwrap();
        assert_eq!(uri, "/body/v1/ws/device/test-device");
        assert_eq!(host, format!("body.example.invalid:{}", address.port()));
        assert_eq!(authorization, "Bearer device-token");
        drop(socket);
        server.await.unwrap();
    }

    #[tokio::test]
    async fn websocket_falls_back_from_configured_address_to_canonical_dns() {
        let (address, observed, server) = websocket_probe().await;
        let url = Url::parse(&format!(
            "ws://localhost:{}/v1/ws/device/test-device",
            address.port()
        ))
        .unwrap();
        let unavailable = SocketAddr::from(([127, 0, 0, 2], address.port()));

        let (socket, actual, source) = dial_bridge(&url, "device-token", &[unavailable])
            .await
            .unwrap();
        assert_eq!(actual, address);
        assert_eq!(source, DialSource::Dns);
        let (_, host, _) = observed.await.unwrap();
        assert_eq!(host, format!("localhost:{}", address.port()));
        drop(socket);
        server.await.unwrap();
    }

    #[tokio::test]
    async fn response_is_enqueued_before_the_spool_ack() {
        let root =
            std::env::temp_dir().join(format!("praxis-transport-order-{}", uuid::Uuid::new_v4()));
        let runtime = Runtime::open(BodyConfig {
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
        })
        .unwrap();
        let (tx, mut rx) = mpsc::channel(2);
        enqueue_response_then_ack(
            &runtime,
            &tx,
            Frame::Result {
                request_id: "request".into(),
                operation_id: "operation".into(),
                status: OperationStatus::Succeeded,
                ok: true,
                result: serde_json::json!({"ok": true}),
                artifacts: Vec::new(),
            },
            41,
        )
        .await
        .unwrap();

        let response = frame(rx.recv().await.unwrap());
        let ack = frame(rx.recv().await.unwrap());
        assert!(matches!(response.frame, Frame::Result { .. }));
        assert!(matches!(ack.frame, Frame::Ack { through_seq: 41 }));
        assert!(response.seq < ack.seq);

        enqueue_response_then_ack(
            &runtime,
            &tx,
            Frame::Accepted {
                request_id: "process-request".into(),
                operation_id: "process-operation".into(),
                status: OperationStatus::Starting,
                identity: crate::identity::current(),
            },
            42,
        )
        .await
        .unwrap();
        let accepted = frame(rx.recv().await.unwrap());
        let accepted_ack = frame(rx.recv().await.unwrap());
        assert!(matches!(accepted.frame, Frame::Accepted { .. }));
        assert!(matches!(accepted_ack.frame, Frame::Ack { through_seq: 42 }));
        assert!(accepted.seq < accepted_ack.seq);

        drop(runtime);
        let _ = std::fs::remove_dir_all(root);
    }
}
