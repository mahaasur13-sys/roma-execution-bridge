#!/usr/bin/env python3
"""
ROMA Beta Invitation Sender
Reads leads from SQLite (or CSV), sends personalized invitations via SendGrid.
"""

import csv
import os
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_DIR))

SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY", "")
FROM_EMAIL = os.environ.get("FROM_EMAIL", "beta@roma-execution-bridge.io")
DASHBOARD_URL = "https://roma-execution-bridge-asurdev.zocomputer.io/dashboard?api_key=roma-demo-key-2026"
BATCH_SIZE = 10
DELAY_SECONDS = 6  # 10 emails/min = 1 every 6 seconds
MAX_RETRIES = 3


def load_leads_from_db():
    import db
    db.init_db()
    return db.list_leads(status="new")


def load_leads_from_csv(csv_path: str):
    leads = []
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            leads.append({
                "email": row.get("email", "").strip(),
                "company": row.get("company", "").strip(),
                "role": row.get("role", "").strip(),
                "use_case": row.get("use_case", "").strip(),
            })
    return leads


def generate_invitation_html(recipient_name: str, email: str, invitation_link: str) -> str:
    name = recipient_name or "there"
    return f"""<html>
<body style="font-family: Arial, sans-serif; color: #e5e7eb; background: #0f1117; padding: 20px;">
<div style="max-width: 600px; margin: 0 auto; background: #161b22; border-radius: 12px; padding: 40px;">
    <h1 style="color: #f9fafb;">🚀 Welcome to ROMA Beta, {name}!</h1>

    <p style="color: #8b949e; line-height: 1.6;">
        Thanks for signing up. ROMA helps ML teams run GPU workloads with built-in cost tracking,
        multi-tenancy, and Stripe billing — all from a single dashboard.
    </p>

    <div style="text-align: center; margin: 30px 0;">
        <a href="{invitation_link}&email={email}&ref=beta" 
           style="background: #238636; color: #fff; padding: 14px 32px; border-radius: 8px; text-decoration: none; font-size: 16px; font-weight: bold;">
           Open Dashboard
        </a>
    </div>

    <p style="color: #8b949e; font-size: 13px;">Or paste this in your browser:</p>
    <code style="background: #0d1117; padding: 8px; display: block; border-radius: 4px; word-break: break-all;">
        {invitation_link}&email={email}
    </code>

    <div style="margin-top: 30px; padding-top: 20px; border-top: 1px solid #30363d;">
        <p style="color: #8b949e; font-size: 13px;">
            <strong>Quick start:</strong><br>
            1. Open the dashboard<br>
            2. Click "Hello World" demo<br>
            3. Try submitting your own task<br><br>
            <strong>Docs:</strong> <a href="https://github.com/mahaasur13-sys/roma-execution-bridge/tree/master/docs" style="color: #3b82f6;">docs/</a><br>
            <strong>Questions?</strong> Reply to this email.
        </p>
    </div>
</div>
</body>
</html>"""


def send_email_via_sendgrid(to_email: str, to_name: str, html_content: str, retries: int = MAX_RETRIES) -> dict:
    if not SENDGRID_API_KEY:
        return {"success": False, "error": "SENDGRID_API_KEY not set"}

    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail, Email, To, Content

        sg = SendGridAPIClient(SENDGRID_API_KEY)
        message = Mail(
            from_email=Email(FROM_EMAIL, "ROMA Team"),
            to_emails=To(to_email, to_name),
            subject="You're invited — ROMA Beta (GPU Execution Platform)",
            html_content=Content("text/html", html_content),
        )
        message.add_category("beta-invitation")

        for attempt in range(retries):
            try:
                response = sg.send(message)
                if response.status_code in (200, 201, 202):
                    return {"success": True}
                time.sleep(2)
            except Exception as e:
                if attempt == retries - 1:
                    return {"success": False, "error": str(e)}
                time.sleep(2)

        return {"success": False, "error": f"HTTP {response.status_code}"}

    except Exception as e:
        return {"success": False, "error": str(e)}


def main():
    import db
    db.init_db()

    # Load leads
    leads = load_leads_from_db()
    if not leads:
        csv_path = PROJECT_DIR / "data" / "beta_leads_template.csv"
        if csv_path.exists():
            print(f"No leads in DB. Loading from {csv_path}...")
            leads = load_leads_from_csv(str(csv_path))
            # Seed into DB
            for lead in leads:
                db.add_lead(
                    email=lead["email"],
                    company=lead.get("company", ""),
                    role=lead.get("role", ""),
                    use_case=lead.get("use_case", ""),
                    source=lead.get("source", ""),
                )
            leads = load_leads_from_db()
            print(f"Seeded {len(leads)} leads from CSV")
        else:
            print("No leads found.")
            return

    if not leads:
        print("No leads to invite.")
        return

    print(f"Preparing to send {len(leads)} invitations...")
    sent_count = 0
    fail_count = 0

    for i, lead in enumerate(leads):
        email = lead.get("email", "")
        if not email:
            continue

        name = lead.get("company", "") or lead.get("role", "") or ""
        link = DASHBOARD_URL
        html = generate_invitation_html(name, email, link)

        result = send_email_via_sendgrid(email, name, html)
        if result["success"]:
            db.log_email_sent(email, name, lead.get("id", ""), f"{link}&email={email}")
            db.update_lead_status(lead["id"], "contacted")
            sent_count += 1
            print(f"  [{i+1}/{len(leads)}] ✅ {email}")
        else:
            db.log_email_failed(email, result.get("error", "Unknown error"))
            fail_count += 1
            print(f"  [{i+1}/{len(leads)}] ❌ {email} — {result.get('error')}")

        # Rate limiting: 10 emails/min
        if (i + 1) % BATCH_SIZE == 0 and i + 1 < len(leads):
            print(f"  ⏸ Pausing {DELAY_SECONDS}s (rate limit)...")
            time.sleep(DELAY_SECONDS)

    print(f"\n=== Done ===")
    print(f"Sent: {sent_count}, Failed: {fail_count}")

    stats = db.get_email_stats()
    print(f"Stats: {stats}")


if __name__ == "__main__":
    main()
