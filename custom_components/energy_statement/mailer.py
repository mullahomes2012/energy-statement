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


def lookup_dns_over_https(host: str) -> list[str]:
    """IPv4 addresses for host from Cloudflare or Google DNS-over-HTTPS.

    Used when the network's own DNS sends mail servers to the wrong place
    (some routers and filters answer for gmail/outlook with an address that
    cuts encrypted connections). DNS-over-HTTPS can't be redirected that way.
    """
    import json
    import urllib.parse
    import urllib.request

    name = urllib.parse.quote(host.strip().rstrip("."))
    for url in (
        f"https://cloudflare-dns.com/dns-query?name={name}&type=A",
        f"https://dns.google/resolve?name={name}&type=A",
    ):
        try:
            request = urllib.request.Request(url, headers={"accept": "application/dns-json"})
            with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
                data = json.loads(response.read().decode())
            addresses = [a["data"] for a in data.get("Answer", []) if a.get("type") == 1]
            if addresses:
                return addresses
        except (OSError, ValueError):
            continue
    return []


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
    """Name check for connections made without SNI.

    The SSL context still verifies the whole certificate chain; this adds the
    check that the certificate is for the server we meant to reach.
    """
    cert = sock.getpeercert()
    if not cert or not _hostname_matches(cert, host):
        raise ssl.SSLCertVerificationError(f"certificate is not valid for {host}")


def _connect_to(addresses: list[str] | None, host: str, port: int, timeout: float, source_address=None) -> socket.socket:
    if not addresses:
        return _connect_ipv4_first(host, port, timeout, source_address)
    last_error: OSError | None = None
    for address in addresses:
        try:
            return socket.create_connection((address, port), timeout, source_address)
        except OSError as err:
            last_error = err
    raise last_error or OSError(f"Couldn't connect to {host}")


class _SMTP(smtplib.SMTP):
    def __init__(self, *args, addresses: list[str] | None = None, no_sni: bool = False, **kwargs):
        self.addresses = addresses
        self.no_sni = no_sni
        super().__init__(*args, **kwargs)

    def _get_socket(self, host, port, timeout):
        return _connect_to(self.addresses, host, port, timeout, self.source_address)

    def starttls(self, *, context=None):
        if not self.no_sni:
            return super().starttls(context=context)
        host = self._host
        self._host = None  # encrypt without announcing the server name
        try:
            reply = super().starttls(context=context)
        finally:
            self._host = host
        _verify_host(self.sock, host)
        return reply


class _SMTP_SSL(smtplib.SMTP_SSL):
    def __init__(self, *args, addresses: list[str] | None = None, no_sni: bool = False, **kwargs):
        self.addresses = addresses
        self.no_sni = no_sni
        super().__init__(*args, **kwargs)

    def _get_socket(self, host, port, timeout):
        sock = _connect_to(self.addresses, host, port, timeout, self.source_address)
        if not self.no_sni:
            return self.context.wrap_socket(sock, server_hostname=self._host)
        wrapped = self.context.wrap_socket(sock, server_hostname=None)
        _verify_host(wrapped, host)
        return wrapped


class MailError(Exception):
    """Sending failed; message is safe to show to the user."""


def _open(cfg: dict, addresses: list[str] | None, no_sni: bool = False) -> smtplib.SMTP:
    host = cfg["smtp_host"].strip()
    port = int(cfg["smtp_port"])
    security = cfg.get("smtp_security", SECURITY_STARTTLS)
    context = ssl.create_default_context()
    if no_sni:
        context.check_hostname = False  # done by _verify_host instead
    if security == SECURITY_SSL:
        server: smtplib.SMTP = _SMTP_SSL(
            host, port, timeout=TIMEOUT, context=context, addresses=addresses, no_sni=no_sni
        )
    else:
        server = _SMTP(host, port, timeout=TIMEOUT, addresses=addresses, no_sni=no_sni)
        server.ehlo()
        if security == SECURITY_STARTTLS:
            server.starttls(context=context)
            server.ehlo()
    if cfg.get("smtp_username"):
        server.login(cfg["smtp_username"], cfg.get("smtp_password", ""))
    return server


def _connect(cfg: dict) -> smtplib.SMTP:
    """Connect and log in.

    Some networks block the big mail providers: their DNS hands out a wrong
    address and/or connections announcing e.g. smtp.gmail.com are cut off.
    If the first attempt is cut off as encryption starts, try again at the
    address from DNS-over-HTTPS, first announcing the name, then without it
    (the certificate is still fully checked either way).
    """
    retryable = (smtplib.SMTPServerDisconnected, smtplib.SMTPNotSupportedError, ssl.SSLError, ConnectionResetError)
    try:
        try:
            return _open(cfg, None)
        except retryable as first:
            if cfg.get("smtp_security", SECURITY_STARTTLS) == SECURITY_NONE:
                raise
            addresses = lookup_dns_over_https(cfg["smtp_host"]) or None
            attempts = [(addresses, False), (addresses, True)] if addresses else [(None, True)]
            error: Exception = first
            for addrs, no_sni in attempts:
                try:
                    return _open(cfg, addrs, no_sni)
                except smtplib.SMTPAuthenticationError:
                    raise
                except retryable as err:
                    error = err
            raise error
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
