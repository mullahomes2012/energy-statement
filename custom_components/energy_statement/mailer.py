"""SMTP email sending (blocking - run in an executor). No Home Assistant imports."""

from __future__ import annotations

import smtplib
import socket
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from .const import SECURITY_NONE, SECURITY_SSL, SECURITY_STARTTLS

TIMEOUT = 30


def _connect_ipv4_first(host: str, port: int, timeout: float, source_address=None) -> socket.socket:
    """Open a TCP connection, trying IPv4 addresses before IPv6.

    Some home networks have IPv6 routes that break encrypted mail traffic
    (connections to Gmail/Outlook drop as soon as TLS starts), so IPv4 goes
    first. IPv6 is still used when a server has no IPv4 address.
    """
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    infos.sort(key=lambda info: 0 if info[0] == socket.AF_INET else 1)
    last_error: OSError | None = None
    for family, kind, proto, _, address in infos:
        sock = socket.socket(family, kind, proto)
        sock.settimeout(timeout)
        try:
            if source_address:
                sock.bind(source_address)
            sock.connect(address)
            return sock
        except OSError as err:
            last_error = err
            sock.close()
    raise last_error or OSError(f"Couldn't find an address for {host}")


def _hostname_matches(cert: dict, host: str) -> bool:
    """Does the certificate cover this host name (including *.wildcards)?"""
    host = host.lower().rstrip(".")
    for kind, name in cert.get("subjectAltName", ()):
        if kind != "DNS":
            continue
        name = name.lower()
        if name == host:
            return True
        if name.startswith("*.") and "." in host and host.split(".", 1)[1] == name[2:]:
            return True
    return False


def _verify_host(sock: ssl.SSLSocket, host: str) -> None:
    """Check the certificate names the server we meant to reach.

    Used when connecting without SNI: the certificate chain is still verified
    by the SSL context; this adds the host name check that SNI mode skips.
    """
    cert = sock.getpeercert()
    if not cert or not _hostname_matches(cert, host):
        raise ssl.SSLCertVerificationError(f"certificate is not valid for {host}")


class _SMTP(smtplib.SMTP):
    def __init__(self, *args, no_sni: bool = False, **kwargs):
        self.no_sni = no_sni
        super().__init__(*args, **kwargs)

    def _get_socket(self, host, port, timeout):
        return _connect_ipv4_first(host, port, timeout, self.source_address)

    def starttls(self, *, context=None):
        if not self.no_sni:
            return super().starttls(context=context)
        host = self._host
        self._host = None  # wrap without announcing the server name
        try:
            reply = super().starttls(context=context)
        finally:
            self._host = host
        _verify_host(self.sock, host)
        return reply


class _SMTP_SSL(smtplib.SMTP_SSL):
    def __init__(self, *args, no_sni: bool = False, **kwargs):
        self.no_sni = no_sni
        super().__init__(*args, **kwargs)

    def _get_socket(self, host, port, timeout):
        sock = _connect_ipv4_first(host, port, timeout, self.source_address)
        if not self.no_sni:
            return self.context.wrap_socket(sock, server_hostname=self._host)
        wrapped = self.context.wrap_socket(sock, server_hostname=None)
        _verify_host(wrapped, host)
        return wrapped


class MailError(Exception):
    """Sending failed; message is safe to show to the user."""


def _open(cfg: dict, no_sni: bool) -> smtplib.SMTP:
    host = cfg["smtp_host"].strip()
    port = int(cfg["smtp_port"])
    security = cfg.get("smtp_security", SECURITY_STARTTLS)
    context = ssl.create_default_context()
    if no_sni:
        context.check_hostname = False  # checked by _verify_host instead
    if security == SECURITY_SSL:
        server: smtplib.SMTP = _SMTP_SSL(host, port, timeout=TIMEOUT, context=context, no_sni=no_sni)
    else:
        server = _SMTP(host, port, timeout=TIMEOUT, no_sni=no_sni)
        server.ehlo()
        if security == SECURITY_STARTTLS:
            server.starttls(context=context)
            server.ehlo()
    if cfg.get("smtp_username"):
        server.login(cfg["smtp_username"], cfg.get("smtp_password", ""))
    return server


def _connect(cfg: dict) -> smtplib.SMTP:
    """Connect and log in. If the connection is cut off as encryption starts
    (some networks block big mail providers by name), try once more without
    announcing the server name."""
    try:
        try:
            return _open(cfg, no_sni=False)
        except (smtplib.SMTPServerDisconnected, ssl.SSLError, ConnectionResetError):
            if cfg.get("smtp_security", SECURITY_STARTTLS) == SECURITY_NONE:
                raise
            return _open(cfg, no_sni=True)
    except smtplib.SMTPAuthenticationError as err:
        raise MailError("The mail server rejected the username or password.") from err
    except ssl.SSLCertVerificationError as err:
        raise MailError(f"The mail server's security certificate didn't check out: {err}") from err
    except (OSError, smtplib.SMTPException) as err:
        raise MailError(f"Couldn't connect to the mail server: {err}") from err


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
