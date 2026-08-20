"""ROMA Alerts — Email channel via SMTP."""

import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

logger = logging.getLogger("roma.alerts.email")


def send_email_alert(
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
    from_addr: str,
    to_addr: str,
    subject: str,
    body: str,
) -> bool:
    """Send alert via SMTP email. Returns True on success."""
    if not smtp_host or not to_addr:
        logger.warning("Email alert skipped: smtp_host or to_addr not configured")
        return False

    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = from_addr
        msg["To"] = to_addr
        msg["Subject"] = f"[ROMA Alert] {subject}"

        plain_body = body
        html_body = f"""<html><body>
<h2>ROMA Execution Bridge — Alert</h2>
<pre style="background:#1e1e1e;color:#d4d4d4;padding:16px;border-radius:8px;font-size:14px">
{body}
</pre>
<p style="color:#888;font-size:12px">ROMA Execution Bridge v2.1.0 — Auto Alert System</p>
</body></html>"""

        msg.attach(MIMEText(plain_body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
            server.starttls()
            if smtp_user and smtp_password:
                server.login(smtp_user, smtp_password)
            server.sendmail(from_addr, [to_addr], msg.as_string())

        logger.info("Email alert sent to %s", to_addr)
        return True
    except Exception as exc:
        logger.error("Email alert failed: %s", exc)
        return False
