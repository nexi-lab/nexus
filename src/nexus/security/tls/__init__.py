"""SSH-style TOFU mTLS for gRPC zone federation (#1250).

Auto-generates X.509 certificates on zone init.  TOFU-pins peer zone CA
fingerprints on first ``nexus mount``, verifies on reconnect.

Public API:
    generate_zone_ca, generate_node_cert, cert_fingerprint,
    save_pem, load_pem_cert, load_pem_key,
    ZoneTlsConfig,
    TofuTrustStore, TofuResult, ZoneCertificateChangedError,
    generate_join_token, parse_join_token, verify_password,
"""

from nexus.security.tls.certgen import (
    cert_fingerprint,
    generate_node_cert,
    generate_zone_ca,
    load_pem_cert,
    load_pem_key,
    save_pem,
)
from nexus.security.tls.config import ZoneTlsConfig
from nexus.security.tls.join_token import (
    generate_join_token,
    parse_join_token,
    verify_password,
)
from nexus.security.tls.trust_store import (
    TofuResult,
    TofuTrustStore,
    ZoneCertificateChangedError,
)

__all__ = [
    "TofuResult",
    "TofuTrustStore",
    "ZoneCertificateChangedError",
    "ZoneTlsConfig",
    "cert_fingerprint",
    "generate_join_token",
    "generate_node_cert",
    "generate_zone_ca",
    "load_pem_cert",
    "load_pem_key",
    "parse_join_token",
    "save_pem",
    "verify_password",
]
