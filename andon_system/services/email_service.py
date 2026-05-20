from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from flask import current_app

logger = logging.getLogger(__name__)


def send_email(subject: str, recipients: list[str], body_text: str) -> bool:
    """Send a plain-text email using SMTP settings from app config."""
    normalized_recipients = sorted({str(item or "").strip() for item in recipients if str(item or "").strip()})
    if not normalized_recipients:
        return False

    if not current_app.config.get("ESCALATION_EMAIL_ENABLED", False):
        logger.debug("Escalation email skipped because ESCALATION_EMAIL_ENABLED is false")
        return False

    smtp_host = (current_app.config.get("ESCALATION_EMAIL_SMTP_HOST") or "").strip()
    smtp_port = int(current_app.config.get("ESCALATION_EMAIL_SMTP_PORT") or 587)
    smtp_user = (current_app.config.get("ESCALATION_EMAIL_SMTP_USER") or "").strip()
    smtp_password = str(current_app.config.get("ESCALATION_EMAIL_SMTP_PASSWORD") or "")
    use_tls = bool(current_app.config.get("ESCALATION_EMAIL_USE_TLS", True))
    use_ssl = bool(current_app.config.get("ESCALATION_EMAIL_USE_SSL", False))
    from_email = (current_app.config.get("ESCALATION_EMAIL_FROM") or smtp_user or "").strip()

    if not smtp_host or not from_email:
        logger.warning("Escalation email is enabled but SMTP host/from email is missing")
        return False

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = from_email
    message["To"] = ", ".join(normalized_recipients)
    message.set_content(body_text)

    try:
        if use_ssl:
            with smtplib.SMTP_SSL(host=smtp_host, port=smtp_port, timeout=20) as client:
                if smtp_user and smtp_password:
                    client.login(smtp_user, smtp_password)
                client.send_message(message)
        else:
            with smtplib.SMTP(host=smtp_host, port=smtp_port, timeout=20) as client:
                client.ehlo()
                if use_tls:
                    client.starttls()
                    client.ehlo()
                if smtp_user and smtp_password:
                    client.login(smtp_user, smtp_password)
                client.send_message(message)
    except Exception:
        logger.exception("Failed to send escalation email")
        return False

    logger.info("Escalation email sent to %s recipient(s)", len(normalized_recipients))
    return True

