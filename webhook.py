import html
import json
import os
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler

import requests
from supabase import create_client

BOT_TOKEN = os.environ["BOT_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")  # optional but recommended

BASE = "https://qimanga.com"
TG = f"https://api.telegram.org/bot{BOT_TOKEN}/"
LOCAL_TZ = timezone(timedelta(hours=8))  # Asia/Manila
STALE_AFTER_MIN = 35  # cron runs every 15 min; allow some slack
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0 Safari/537.36"}

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


def reply(chat_id, text):
    requests.post(
        TG + "sendMessage",
        data={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=15,
    )


def check_site():
    """Live check of the manga site. Returns (ok, detail)."""
    try:
        t = time.time()
        r = requests.get(BASE + "/", headers=HEADERS, timeout=10)
        ms = int((time.time() - t) * 1000)
        if r.ok:
            return True, f"online ({r.status_code}, {ms} ms)"
        return False, f"HTTP {r.status_code} ({ms} ms)"
    except Exception as e:
        return False, f"unreachable ({type(e).__name__})"


def ago(dt):
    secs = int((datetime.now(timezone.utc) - dt).total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60} min ago"
    if secs < 86400:
        return f"{secs // 3600}h {(secs % 3600) // 60}m ago"
    return f"{secs // 86400}d ago"


def status_text():
    site_ok, site_detail = check_site()

    row = None
    try:
        res = supabase.table("bot_status").select("*").eq("id", 1).execute()
        row = res.data[0] if res.data else None
    except Exception as e:
        print("status read failed:", e)

    tracked = "?"
    try:
        res = supabase.table("new_series").select("slug", count="exact").limit(1).execute()
        tracked = res.count if res.count is not None else len(res.data)
    except Exception:
        pass

    lines = ["<b>Bot status</b>", ""]
    lines.append(f"{'🟢' if site_ok else '🔴'} Website: {html.escape(site_detail)}")

    if not row or not row.get("last_run_at"):
        lines.append("🟡 Cron job: no run recorded yet")
    else:
        last = datetime.fromisoformat(row["last_run_at"].replace("Z", "+00:00"))
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        age_min = (datetime.now(timezone.utc) - last).total_seconds() / 60
        local = last.astimezone(LOCAL_TZ).strftime("%b %d, %I:%M %p")
        if age_min > STALE_AFTER_MIN:
            icon, label = "🟠", "stale, cron may have stopped"
        elif row.get("last_error"):
            icon, label = "🔴", "last run failed"
        else:
            icon, label = "🟢", "running"
        lines.append(f"{icon} Cron job: {label}")
        lines.append(f"   Last run: {local} ({ago(last)})")
        if row.get("last_error"):
            lines.append(f"   Error: {html.escape(row['last_error'])}")
        elif row.get("last_result"):
            lines.append(f"   Result: {html.escape(row['last_result'])}")

    lines.append(f"📚 Series tracked: {tracked}")
    return "\n".join(lines)


HELP = (
    "<b>Clyde Updater bot</b>\n\n"
    "I post new series from qimanga.com as soon as they appear.\n\n"
    "/status - website and cron job health\n"
    "/help - show this message"
)


class handler(BaseHTTPRequestHandler):
    def _ok(self, code=200, body=b"ok"):
        self.send_response(code)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._ok(body=b"webhook alive")

    def do_POST(self):
        if WEBHOOK_SECRET and self.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
            return self._ok(401, b"unauthorized")
        try:
            length = int(self.headers.get("Content-Length", 0))
            update = json.loads(self.rfile.read(length) or b"{}")
            msg = update.get("message") or {}
            text = (msg.get("text") or "").strip()
            chat_id = (msg.get("chat") or {}).get("id")
            if chat_id and text.startswith("/"):
                cmd = text.split()[0].split("@")[0].lower()
                if cmd in ("/start", "/help"):
                    reply(chat_id, HELP)
                elif cmd == "/status":
                    reply(chat_id, status_text())
        except Exception as e:
            print("webhook error:", e)
        self._ok()  # always 200 so Telegram doesn't retry endlessly