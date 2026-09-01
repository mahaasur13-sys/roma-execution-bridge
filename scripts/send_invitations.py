#!/usr/bin/env python3
"""
ROMA Beta Invitation Sender
Reads leads from SQLite, sends personalized invitations via SendGrid.
Supports --dry-run and --limit flags. Logs to logs/invitations.log.
"""

import argparse
import csv
import logging
import os
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_DIR))

# --- Config ---
SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY", "")
FROM_EMAIL = os.environ.get("FROM_EMAIL", "beta@roma-execution-bridge.io")
DEMO_API_KEY = os.environ.get("ROMA_DEMO_API_KEY", "")
DASHBOARD_URL = "https://roma-execution-bridge-asurdev.zocomputer.io/dashboard"
BATCH_SIZE = 10
DELAY_SECONDS = 6
MAX_RETRIES = 3

# --- Logging ---
LOG_DIR = PROJECT_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

logger = logging.getLogger("invitations")
logger.setLevel(logging.DEBUG)

fh = logging.FileHandler(LOG_DIR / "invitations.log", encoding="utf-8")
fh.setLevel(logging.DEBUG)
fh.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
))

ch = logging.StreamHandler(sys.stdout)
ch.setLevel(logging.INFO)
ch.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))

logger.addHandler(fh)
logger.addHandler(ch)


# ============================================
# EMAIL TEMPLATES
# ============================================

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>
    body { font-family: 'Segoe UI', Arial, sans-serif; background: #0d1117; color: #c9d1d9; margin: 0; padding: 20px; }
    .container { max-width: 600px; margin: 0 auto; background: #161b22; padding: 30px; border-radius: 12px; border: 1px solid #30363d; }
    h1 { color: #58a6ff; font-size: 28px; margin-bottom: 10px; }
    .highlight { color: #f0883e; }
    .button { display: inline-block; background: #238636; color: #fff; padding: 14px 28px; text-decoration: none; border-radius: 6px; font-weight: bold; }
    .button:hover { background: #2ea043; }
    .code { background: #0d1117; padding: 8px 12px; border-radius: 4px; font-family: monospace; border: 1px solid #30363d; }
    .features { list-style: none; padding: 0; }
    .features li { padding: 4px 0; }
    .footer { margin-top: 30px; font-size: 12px; color: #8b949e; border-top: 1px solid #30363d; padding-top: 20px; }
  </style>
</head>
<body>
  <div class="container">
    <h1>🚀 ROMA Execution Bridge</h1>
    <p>Здравствуйте, {{ name }},</p>
    <p>Мы рады пригласить вас в <strong>закрытое бета-тестирование</strong> <span class="highlight">ROMA Execution Bridge</span> — платформы для управления GPU-вычислениями с умным планированием, прогнозированием затрат и изоляцией ресурсов.</p>
    <p><strong>Ваш демо-ключ:</strong> <span class="code">{{ demo_key }}</span></p>
    <p>Начните работу за 2 минуты:</p>
    <p><a href="{{ invitation_link }}" class="button">👉 Открыть дашборд</a></p>
    <p>Вы сможете:</p>
    <ul class="features">
      <li>✅ Отправлять задачи через API или интерфейс</li>
      <li>✅ Отслеживать использование с помощью графиков</li>
      <li>✅ Запускать демо-задачи (PyTorch, инференс, бенчмарки)</li>
      <li>✅ Оставлять обратную связь прямо из дашборда</li>
    </ul>
    <p>Ваша обратная связь бесценна — используйте форму в дашборде или ответьте на это письмо.</p>
    <p>Если есть вопросы: <a href="mailto:support@roma-execution-bridge.io" style="color:#58a6ff;">support@roma-execution-bridge.io</a></p>
    <p>Спасибо, что помогаете развивать ROMA!</p>
    <p>— Команда ROMA</p>
    <div class="footer">
      Это письмо отправлено на {{ email }}. Если вы не запрашивали приглашение, проигнорируйте его.<br>
      &copy; 2026 ROMA Execution Bridge
    </div>
  </div>
</body>
</html>"""

PLAIN_TEXT_TEMPLATE = """Здравствуйте, {{ name }}!

Мы рады пригласить вас в закрытое бета-тестирование ROMA Execution Bridge — платформы для управления GPU-вычислениями.

Ваш демо-ключ: {{ demo_key }}

Начните работу: {{ invitation_link }}

Возможности:
- Отправлять задачи через API или интерфейс
- Отслеживать использование с помощью графиков
- Запускать демо-задачи
- Оставлять обратную связь прямо в дашборде

Вопросы? Ответьте на это письмо.

— Команда ROMA"""


# ============================================
# HELPERS
# ============================================

def _render(template: str, name: str, email: str, invitation_link: str, demo_key: str) -> str:
    return template \
        .replace("{{ name }}", name or "Valued Tester") \
        .replace("{{ email }}", email) \
        .replace("{{ invitation_link }}", invitation_link) \
        .replace("{{ demo_key }}", demo_key)


def generate_email_content(recipient_name: str, email: str, invitation_link: str, demo_key: str = "") -> tuple[str, str]:
    """Return (html_body, plain_text_body)."""
    name = recipient_name or "Valued Tester"
    html = _render(HTML_TEMPLATE, name, email, invitation_link, demo_key)
    text = _render(PLAIN_TEXT_TEMPLATE, name, email, invitation_link, demo_key)
    return html, text


def load_leads_from_db(limit: int = 0):
    import db
    db.init_db()
    leads = db.list_leads(status="new")
    if limit and len(leads) > limit:
        leads = leads[:limit]
    return leads


def load_leads_from_csv(csv_path: str):
    leads = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            leads.append({
                "id": None,
                "email": row.get("email", "").strip(),
                "company": row.get("company", "").strip(),
                "role": row.get("role", "").strip(),
                "use_case": row.get("use_case", "").strip(),
            })
    return leads


# ============================================
# SENDGRID SENDER
# ============================================

def send_email_via_sendgrid(to_email: str, to_name: str, html_content: str) -> dict:
    """Send via SendGrid API with exponential backoff."""
    if not SENDGRID_API_KEY:
        return {"success": False, "error": "SENDGRID_API_KEY not set"}

    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail, Email, To, Content
    except ImportError:
        return {"success": False, "error": "sendgrid package not installed (pip install sendgrid)"}

    sg = SendGridAPIClient(SENDGRID_API_KEY)
    message = Mail(
        from_email=Email(FROM_EMAIL, "ROMA Team"),
        to_emails=To(to_email, to_name),
        subject="Вы приглашены — ROMA Beta (GPU Execution Platform)",
        html_content=Content("text/html", html_content),
    )
    message.add_category("beta-invitation")

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = sg.send(message)
            if response.status_code in (200, 201, 202):
                logger.debug(f"SendGrid OK: {to_email} (attempt {attempt})")
                return {"success": True}
            logger.warning(f"SendGrid HTTP {response.status_code} for {to_email} (attempt {attempt})")
        except Exception as e:
            logger.warning(f"SendGrid attempt {attempt}/{MAX_RETRIES} failed for {to_email}: {e}")

        if attempt < MAX_RETRIES:
            backoff = 2 ** attempt
            logger.debug(f"Retrying in {backoff}s...")
            time.sleep(backoff)

    return {"success": False, "error": f"Failed after {MAX_RETRIES} attempts"}


# ============================================
# MAIN
# ============================================

def main():
    parser = argparse.ArgumentParser(description="ROMA Beta Invitation Sender")
    parser.add_argument("--limit", type=int, default=0, help="Max emails to send (0 = all)")
    parser.add_argument("--dry-run", action="store_true", default=False,
                        help="Simulate without sending real emails")
    parser.add_argument("--csv", type=str, default="", help="Load leads from CSV instead of DB")
    args = parser.parse_args()

    import db
    db.init_db()

    # Determine mode
    dry_run = args.dry_run or not SENDGRID_API_KEY
    if not SENDGRID_API_KEY:
        dry_run = True
        logger.warning("⚠ SENDGRID_API_KEY is not set — switching to DRY-RUN mode")
        logger.warning("  Set SENDGRID_API_KEY in .env to send real emails.")

    if not FROM_EMAIL:
        logger.error("FROM_EMAIL is not set. Aborting.")
        sys.exit(1)

    if not DEMO_API_KEY:
        logger.warning("⚠ ROMA_DEMO_API_KEY is not set — invitation links will not include a demo key")

    # Load leads
    if args.csv:
        leads = load_leads_from_csv(args.csv)
        logger.info(f"Loaded {len(leads)} leads from CSV: {args.csv}")
    else:
        leads = load_leads_from_db(limit=args.limit)
        logger.info(f"Loaded {len(leads)} leads from DB (status=new)")

    if not leads:
        logger.info("No leads to invite.")
        return

    # Send
    start_time = time.monotonic()
    sent_count = 0
    fail_count = 0
    skipped = 0

    for i, lead in enumerate(leads):
        email = lead.get("email", "")
        if not email:
            skipped += 1
            continue

        name = lead.get("company", "") or lead.get("role", "") or ""
        invitation_link = (
            f"{DASHBOARD_URL}?api_key={DEMO_API_KEY}"
            f"&ref=beta&utm_source=email&utm_medium=invite&email={email}"
        )

        html, _ = generate_email_content(name, email, invitation_link, DEMO_API_KEY)
        lead_id = lead.get("id")

        if dry_run:
            # Simulate send
            logger.info(
                f"DRY-RUN [{i+1}/{len(leads)}] ✉ {email} ({name or 'no name'}) "
                f"— would send via SendGrid"
            )
            try:
                db.log_email_sent(email, name, "tenant-demo", invitation_link)
                if lead_id:
                    db.update_lead_status(lead_id, "invited")
                sent_count += 1
            except Exception as e:
                logger.error(f"DRY-RUN DB error for {email}: {e}")
                fail_count += 1
        else:
            # Real send
            result = send_email_via_sendgrid(email, name, html)
            if result.get("success"):
                try:
                    db.log_email_sent(email, name, "tenant-demo", invitation_link)
                    if lead_id:
                        db.update_lead_status(lead_id, "invited")
                    sent_count += 1
                    logger.info(f"[{i+1}/{len(leads)}] ✅ {email} ({name})")
                except Exception as e:
                    logger.error(f"DB error for {email}: {e}")
                    fail_count += 1
            else:
                err = result.get("error", "Unknown")
                try:
                    db.log_email_failed(email, err)
                except Exception:
                    pass
                fail_count += 1
                logger.error(f"[{i+1}/{len(leads)}] ❌ {email} — {err}")

        # Rate limiting
        if (i + 1) % BATCH_SIZE == 0 and i + 1 < len(leads):
            logger.debug(f"Rate limit pause: {DELAY_SECONDS}s")
            time.sleep(DELAY_SECONDS)

    # Summary
    elapsed = round(time.monotonic() - start_time, 1)
    logger.info("=" * 50)
    logger.info(f"Campaign finished in {elapsed}s")
    logger.info(f"  Sent:    {sent_count}")
    logger.info(f"  Failed:  {fail_count}")
    logger.info(f"  Skipped: {skipped}")
    logger.info(f"  Total:   {len(leads)}")
    logger.info(f"  Mode:    {'DRY-RUN' if dry_run else 'LIVE'}")

    # Stats
    try:
        stats = db.get_email_stats()
        logger.info(f"  DB stats: sent={stats.get('sent', 0)}, "
                     f"opened={stats.get('opened', 0)}, "
                     f"clicked={stats.get('clicked', 0)}")
    except Exception as e:
        logger.warning(f"Could not retrieve email stats: {e}")

    logger.info("=" * 50)


if __name__ == "__main__":
    main()
