//! Channel creation utility — centralized tonic Endpoint configuration.

use crate::config::{ClientConfig, TlsConfig};
use crate::error::TransportError;

/// Create a tonic Channel to the given endpoint with optional TLS.
///
/// Centralizes Endpoint configuration (timeouts, keepalive, TLS) so
/// each domain crate doesn't reinvent channel setup.
#[allow(clippy::result_large_err)]
pub async fn create_channel(
    endpoint: &str,
    config: &ClientConfig,
) -> Result<tonic::transport::Channel, TransportError> {
    let mut ep = tonic::transport::Endpoint::from_shared(endpoint.to_string())
        .map_err(|e| TransportError::InvalidAddress(format!("{e}")))?
        .connect_timeout(config.connect_timeout)
        .timeout(config.request_timeout);

    if let Some(keepalive) = config.tcp_keepalive {
        ep = ep.tcp_keepalive(Some(keepalive));
    }
    if let Some(interval) = config.http2_keepalive_interval {
        ep = ep.http2_keep_alive_interval(interval);
    }
    if let Some(timeout) = config.http2_keepalive_timeout {
        ep = ep.keep_alive_timeout(timeout);
    }

    if let Some(ref tls) = config.tls {
        ep = apply_tls(ep, tls)?;
    }

    ep.connect().await.map_err(TransportError::Tonic)
}

/// Apply TLS configuration to a tonic Endpoint.
#[allow(clippy::result_large_err)]
fn apply_tls(
    ep: tonic::transport::Endpoint,
    tls: &TlsConfig,
) -> Result<tonic::transport::Endpoint, TransportError> {
    let ca_cert = tonic::transport::Certificate::from_pem(&tls.ca_pem);
    let identity = tonic::transport::Identity::from_pem(&tls.cert_pem, &tls.key_pem);

    let tls_config = tonic::transport::ClientTlsConfig::new()
        .ca_certificate(ca_cert)
        .identity(identity);

    ep.tls_config(tls_config).map_err(TransportError::Tonic)
}
