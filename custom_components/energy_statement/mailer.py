"""SMTP email sending (blocking - run in an executor). No Home Assistant imports."""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from .const import SECURITY_NONE, SECURITY_SSL, SECURITY_STARTTLS

TIMEOUT = 30


class MailError(Exception):
    """Sending failed; message is safe to show to the user."""


def _connect(cfg: dict) -> smtplib.SMTP:
    host = cfg["smtp_host"].strip()
    port = int(cfg["smtp_port"])
    security = cfg.get("smtp_security", SECURITY_STARTTLS)
    context = ssl.create_default_context()
    try:
        if security == SECURITY_SSL:
            server: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT, context=context)
        else:
            server = smtplib.SMTP(host, port, timeout=TIMEOUT)
            server.ehlo()
            if security == SECURITY_STARTTLS:
                server.starttls(context=context)
                server.ehlo()
        if cfg.get("smtp_username"):
            server.login(cfg["smtp_username"], cfg.get("smtp_password", ""))
    except smtplib.SMTPAuthenticationError as err:
        raise MailError("The mail server rejected the username or password.") from err
    except (OSError, smtplib.SMTPException) as err:
        raise MailError(f"Couldn't connect to the mail server: {err}") from err
    return server


def from_address(cfg: dict) -> str:
    return (cfg.get("from_email") or cfg.get("smtp_username") or "").strip()


def test_connection(cfg: dict) -> None:
    """Log in and out again. Raises MailError."""
    server = _connect(cfg)
    try:
        server.quit()
    except smtplib.SMTPException:
        pass


def send_mail(
    cfg: dict,
    to: list[str],
    cc: list[str],
    subject: str,
    text: str,
    html: str | None = None,
    attachment: tuple[str, bytes] | None = None,
) -> None:
    """Send one message. Raises MailError."""
    sender = from_address(cfg)
    if not sender:
        raise MailError("No from address is set.")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((cfg.get("from_name") or "", sender))
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if cfg.get("reply_to"):
        msg["Reply-To"] = cfg["reply_to"]
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1])
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    if attachment:
        name, data = attachment
        msg.add_attachment(data, maintype="application", subtype="pdf", filename=name)

    server = _connect(cfg)
    try:
        server.send_message(msg, from_addr=sender, to_addrs=list(dict.fromkeys(to + cc)))
    except smtplib.SMTPException as err:
        raise MailError(f"The mail server refused the message: {err}") from err
    finally:
        try:
            server.quit()
        except smtplib.SMTPException:
            pass


__all__ = ["MailError", "send_mail", "test_connection", "SECURITY_NONE"]
