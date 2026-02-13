"""Pluggable email sender for verification emails.

Provides a Protocol for email sending and a default LogEmailSender
that logs the verification URL to the console (development mode).
"""

from __future__ import annotations

import logging
from typing import Protocol

logger = logging.getLogger(__name__)


class EmailSenderProtocol(Protocol):
    """Protocol for sending verification emails."""

    def send_verification_email(self, email: str, verification_url: str) -> None:
        """Send a verification email to the user.

        Args:
            email: Recipient email address
            verification_url: URL containing the verification token
        """
        ...


class LogEmailSender:
    """Development email sender — logs verification URL to console."""

    def send_verification_email(self, email: str, verification_url: str) -> None:
        """Log the verification URL instead of sending a real email.

        Args:
            email: Recipient email address
            verification_url: URL containing the verification token
        """
        logger.info(f"[EMAIL VERIFICATION] To: {email} URL: {verification_url}")
