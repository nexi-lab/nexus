//! SMTP delivery and bounded, in-memory admission for verification requests.

use lettre::{
    message::Mailbox, transport::smtp::authentication::Credentials, AsyncSmtpTransport,
    AsyncTransport, Message, Tokio1Executor,
};
use std::{net::IpAddr, sync::Arc, time::Duration};
use tokio::sync::{OwnedSemaphorePermit, Semaphore};
use tonic::Status;
use url::Url;

const CAPACITY: u32 = 8;

pub struct VerificationEmail {
    transport: AsyncSmtpTransport<Tokio1Executor>,
    from: Mailbox,
    verification_url: Url,
    capacity: Arc<Semaphore>,
}

fn loopback(host: &str) -> bool {
    host == "localhost"
        || host
            .trim_matches(['[', ']'])
            .parse::<IpAddr>()
            .is_ok_and(|ip| ip.is_loopback())
}

impl VerificationEmail {
    pub fn new(
        smtp_url: &str,
        from: &str,
        verification_url: &str,
        credentials: Option<(String, String)>,
    ) -> Result<Self, Status> {
        let invalid = || Status::invalid_argument("Invalid email verification configuration");
        let smtp = Url::parse(smtp_url).map_err(|_| invalid())?;
        let host = smtp.host_str().ok_or_else(invalid)?;
        let host = host.trim_matches(['[', ']']);
        if !smtp.username().is_empty()
            || smtp.password().is_some()
            || smtp.query().is_some()
            || smtp.fragment().is_some()
            || !matches!(smtp.path(), "" | "/")
        {
            return Err(invalid());
        }
        let builder = match smtp.scheme() {
            "smtps" => AsyncSmtpTransport::<Tokio1Executor>::relay(host)
                .map_err(|_| invalid())?
                .port(smtp.port().unwrap_or(465)),
            "smtp" if loopback(host) => {
                AsyncSmtpTransport::<Tokio1Executor>::builder_dangerous(host)
                    .port(smtp.port().unwrap_or(25))
            }
            "smtp" => AsyncSmtpTransport::<Tokio1Executor>::starttls_relay(host)
                .map_err(|_| invalid())?
                .port(smtp.port().unwrap_or(587)),
            _ => return Err(invalid()),
        };
        let builder = if let Some((username, password)) = credentials {
            if username.is_empty() || password.is_empty() {
                return Err(invalid());
            }
            builder.credentials(Credentials::new(username, password))
        } else {
            builder
        };
        let verification_url = Url::parse(verification_url).map_err(|_| invalid())?;
        let host = verification_url.host_str().ok_or_else(invalid)?;
        if !(verification_url.scheme() == "https"
            || (verification_url.scheme() == "http" && loopback(host)))
            || !verification_url.username().is_empty()
            || verification_url.password().is_some()
            || verification_url.query().is_some()
            || verification_url.fragment().is_some()
        {
            return Err(invalid());
        }
        Ok(Self {
            // No SMTP pool: cancellation closes the connection instead of reusing
            // a partially completed SMTP transaction.
            transport: builder.timeout(Some(Duration::from_secs(5))).build(),
            from: from.parse().map_err(|_| invalid())?,
            verification_url,
            capacity: Arc::new(Semaphore::new(CAPACITY as usize)),
        })
    }

    pub(crate) fn admit(&self) -> Result<OwnedSemaphorePermit, Status> {
        Arc::clone(&self.capacity)
            .try_acquire_owned()
            .map_err(|_| Status::resource_exhausted("Verification requests are busy; retry later"))
    }

    pub(crate) async fn send(&self, email: &str, token: &str) -> Result<(), Status> {
        let mut url = self.verification_url.clone();
        url.query_pairs_mut().append_pair("token", token);
        let message = Message::builder()
            .from(self.from.clone())
            .to(Mailbox::new(None, email.parse().map_err(|_| {
                Status::invalid_argument("Invalid email address")
            })?))
            .subject("Verify your Nexus email address")
            .body(format!(
                "Verify your Nexus email address:\r\n{url}\r\n\r\nThis link expires in 24 hours.\r\n"
            ))
            .map_err(|_| Status::internal("Could not prepare verification email"))?;
        tokio::time::timeout(Duration::from_secs(10), self.transport.send(message))
            .await
            .map_err(|_| Status::unavailable("Email delivery timed out"))?
            .map_err(|_| Status::unavailable("Email delivery failed"))?;
        Ok(())
    }

    pub(crate) async fn drain(&self) {
        let _permit = Arc::clone(&self.capacity)
            .acquire_many_owned(CAPACITY)
            .await;
    }
}

pub(crate) fn valid_email(email: &str) -> bool {
    email.len() <= 255 && email.parse::<lettre::Address>().is_ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn configuration_requires_safe_endpoints_and_bare_recipient_addresses() {
        for smtp in [
            "http://127.0.0.1",
            "smtp://user:secret@localhost",
            "smtp://localhost?tls=none",
            "smtp://localhost/other",
        ] {
            assert!(VerificationEmail::new(
                smtp,
                "nexus@example.test",
                "https://example.test/verify",
                None
            )
            .is_err());
        }
        for page in [
            "http://example.test/verify",
            "https://user:secret@example.test",
            "https://example.test?token=old",
            "https://example.test/#old",
        ] {
            assert!(VerificationEmail::new(
                "smtp://127.0.0.1:2525",
                "nexus@example.test",
                page,
                None
            )
            .is_err());
        }
        for address in [
            "bad",
            "Alice <alice@example.test>",
            "alice@example.test\r\nBcc: bob@example.test",
        ] {
            assert!(!valid_email(address));
        }
        assert!(valid_email("alice@example.test"));
        let sender = VerificationEmail::new(
            "smtp://127.0.0.1:2525",
            "nexus@example.test",
            "http://127.0.0.1/verify",
            None,
        )
        .unwrap();
        let permits: Vec<_> = (0..CAPACITY).map(|_| sender.admit().unwrap()).collect();
        assert_eq!(
            sender.admit().unwrap_err().code(),
            tonic::Code::ResourceExhausted
        );
        drop(permits);
        assert!(sender.admit().is_ok());
    }
}
