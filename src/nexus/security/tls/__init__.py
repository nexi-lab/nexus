"""SSH-style mTLS for gRPC zone federation (#1250).

Auto-generates X.509 certificates on zone init. The TOFU peer-CA trust
store is provided by ``nexus_kernel.PyTofuTrustStore``.
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

__all__ = [
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
