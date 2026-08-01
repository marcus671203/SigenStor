#!/usr/bin/env python3
"""
Watchdog for Sigen system.
Runs hourly via systemd timer.
Checks data flows and savings, mails on problems.
"""

import os
import sys
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent

# Load .env manually
for line in (ROOT / ".env").read_text().splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, v = line.split("=", 1)
        os.environ[k.strip()] = v.strip().strip('"').strip("'")

sys.path.insert(0, str(ROOT / "scripts"))
from db import connect

TZ = ZoneInfo("Europe/Stockholm")
GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_PW = os.environ["GMAIL_APP_PASSWORD"]
ALERT_TO = "marcuslif@gmail.com"
COOLDOWN_HOURS = 1


def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def already_alerted(conn, alert_type):
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=COOLDOWN_HOURS)).isoformat()
    row = conn.execute(
        "SELECT id FROM alerts WHERE alert_type = ? AND mail_sent_at > ? "
        "AND resolved_at IS NULL LIMIT 1",
        (alert_type, cutoff)
    ).fetchone()
    return row is not None


def send_mail(subject, body):
    msg = EmailMessage()
    msg["From"] = GMAIL_USER
    msg["To"] = ALERT_TO
    msg["Subject"] = subject
    msg.set_content(body)
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as s:
        s.login(GMAIL_USER, GMAIL_PW)
        s.send_message(msg)


def raise_alert(conn, alert_type, severity, title, message):
    should_mail = not already_alerted(conn, alert_type)
    mail_sent_at = None
    if should_mail:
        try:
            send_mail("[Sigen larm] " + title, message)
            mail_sent_at = now_utc()
            print("MAIL:", title)
        except Exception as e:
            print("Mail failed:", e)
    conn.execute(
        "INSERT INTO alerts (ts_utc, alert_type, severity, title, message, mail_sent_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (now_utc(), alert_type, severity, title, message, mail_sent_at)
    )
    print("ALERT", severity.upper(), "-", title, ":", message)


def resolve_alerts(conn, alert_type):
    n = conn.execute(
        "UPDATE alerts SET resolved_at = ? WHERE alert_type = ? AND resolved_at IS NULL",
        (now_utc(), alert_type)
    ).rowcount
    if n > 0:
        print("OK: resolved", n, "previous", alert_type, "alerts")


def check_realtime_freshness(conn):
    row = conn.execute("SELECT MAX(ts_utc) as senaste FROM realtime").fetchone()
    if not row or not row["senaste"]:
        raise_alert(conn, "realtime_missing", "critical",
                    "Ingen realtime-data",
                    "Realtime-tabellen ar tom. Kolla sigen-realtime-poller.service.")
        return
    senaste_str = row["senaste"]
    senaste = datetime.fromisoformat(senaste_str.replace("Z", "+00:00"))
    if senaste.tzinfo is None:
        senaste = senaste.replace(tzinfo=timezone.utc)
    alder_min = (datetime.now(timezone.utc) - senaste).total_seconds() / 60
    if alder_min > 30:
        raise_alert(conn, "realtime_stale", "critical",
                    "Realtime-data " + str(int(alder_min)) + " min gammal",
                    "Senaste realtime-matning: " + senaste_str +
                    " (" + str(int(alder_min)) + " min sedan). Kolla poller-servicen.")
    else:
        resolve_alerts(conn, "realtime_stale")
        resolve_alerts(conn, "realtime_missing")


def check_5min_freshness(conn):
    row = conn.execute("SELECT MAX(ts_local) as senaste FROM energy_5min_v2").fetchone()
    if not row or not row["senaste"]:
        raise_alert(conn, "5min_missing", "critical",
                    "Ingen 5-min-data", "energy_5min_v2 ar tom.")
        return
    senaste_str = row["senaste"]
    senaste = datetime.fromisoformat(senaste_str).replace(tzinfo=TZ)
    alder_min = (datetime.now(TZ) - senaste).total_seconds() / 60
    if alder_min > 20:
        raise_alert(conn, "5min_stale", "critical",
                    "5-min-data " + str(int(alder_min)) + " min gammal",
                    "Senaste 5-min-fonster: " + senaste_str + " lokal tid.")
    else:
        resolve_alerts(conn, "5min_stale")


def check_savings_anomaly(conn):
    rows = conn.execute("""
        SELECT date, total_kr FROM daily_summary
        WHERE date >= date('now', '-8 days') AND date < date('now')
        ORDER BY date
    """).fetchall()
    if len(rows) < 7:
        return
    igar = rows[-1]
    forra_7 = rows[:-1]
    snitt = sum(r["total_kr"] or 0 for r in forra_7) / len(forra_7)
    if snitt < 10:
        return
    igar_kr = igar["total_kr"] or 0
    diff_procent = (igar_kr - snitt) / snitt * 100
    if diff_procent < -50:
        raise_alert(conn, "savings_low", "warning",
                    "Igar-besparing " + str(int(diff_procent)) + " procent under snitt",
                    "Igar (" + igar["date"] + "): " + str(round(igar_kr, 0)) + " kr. "
                    "7-dagars snitt: " + str(round(snitt, 0)) + " kr.")
    else:
        resolve_alerts(conn, "savings_low")


def main():
    print("=== Watchdog", datetime.now(TZ).isoformat(timespec="seconds"), "===")
    with connect() as conn:
        check_realtime_freshness(conn)
        check_5min_freshness(conn)
        check_savings_anomaly(conn)
    print("=== Watchdog klar ===")


if __name__ == "__main__":
    main()

