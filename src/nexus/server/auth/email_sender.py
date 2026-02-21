"""Pluggable email sender for verification emails.

Provides a default LogEmailSender that logs the verification URL
to the console (development mode).
"""

import logging

logger = logging.getLogger(__name__)


class LogEmailSender:
    """Development email sender — logs verification URL to console."""

    def send_verification_email(self, email: str, verification_url: str) -> None:
        """Log the verification URL instead of sending a real email.

        Args:
            email: Recipient email address
            verification_url: URL containing the verification token
        """
        logger.info(f"[EMAIL VERIFICATION] To: {email} URL: {verification_url}")
