import html
import json
import os
import re
import time
from http.server import BaseHTTPRequestHandler
from difflib import SequenceMatcher
import requests
from bs4 import BeautifulSoup
from supabase import create_client
from datetime import datetime, timedelta, timezone

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
CRON_SECRET = os.environ.get("CRON_SECRET")  # protects the endpoint

BASE = "https://qimanga.com"
MAX_PER_RUN = 3  # keep each run short; leftovers are sent on the next run
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": BASE + "/",
}
TG = f"https://api.telegram.org/bot{BOT_TOKEN}/"
SERIES_RE = re.compile(r"/series/([^/?#]+)/?$")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")  # optional
LOCAL_TZ = timezone(timedelta(hours=8))  # Asia/Manila
STALE_AFTER_MIN = 35  # cron runs every 15 min; allow some slack
FAIL_ALERT_AFTER = 3  # failed runs in a row before you get a Telegram alert

# ---------- Supabase ----------
def load_seen():
    res = supabase.table("new_series").select("slug").execute()
    return {row["slug"] for row in res.data}


def mark_seen(slug, d=None):
    row = {"slug": slug}
    if d:  # remember the cover + synopsis so renamed copies can be recognised
        row["image"] = d["image"]
        row["synopsis"] = d["synopsis"]
    supabase.table("new_series").upsert(row).execute()


def _norm(s):
    return re.sub(r"\W+", " ", (s or "").lower()).strip()


def is_duplicate(d):
    """True if this series was already sent under a different slug/title."""
    res = supabase.table("new_series").select("slug,image,synopsis").execute()
    syn = _norm(d["synopsis"])
    for row in res.data:
        if d["image"] and row.get("image") == d["image"]:
            return True
        old = _norm(row.get("synopsis"))
        if len(syn) > 40 and len(old) > 40 and SequenceMatcher(None, syn, old).ratio() >= 0.85:
            return True
    return False

def record_status(result=None, error=None):
    """Save the run outcome and send a Telegram alert after repeated failures."""
    try:
        res = supabase.table("bot_status").select("fail_count,alerted").eq("id", 1).execute()
        prev = res.data[0] if res.data else {}
        fails = prev.get("fail_count") or 0
        alerted = bool(prev.get("alerted"))

        if error:
            fails += 1
            if fails >= FAIL_ALERT_AFTER and not alerted:
                tg_reply(
                    CHAT_ID,
                    f"⚠️ <b>Bot problem</b>\n{fails} failed runs in a row.\n"
                    f"Last error: {html.escape(error[:200])}",
                )
                alerted = True
        else:
            if alerted:
                tg_reply(CHAT_ID, "✅ <b>Bot recovered</b>, runs are working again.")
            fails, alerted = 0, False

        supabase.table("bot_status").upsert({
            "id": 1,
            "last_run_at": datetime.now(timezone.utc).isoformat(),
            "last_result": result,
            "last_error": error,
            "site_ok": error is None,
            "fail_count": fails,
            "alerted": alerted,
        }).execute()
    except Exception as e:
        print("Could not record status:", e)

# ---------- Scraping ----------
def get_soup(url):
    r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def get_new_series():
    soup = get_soup(BASE + "/")
    heading = soup.find(
        lambda t: t.name in ("h1", "h2", "h3", "h4", "h5", "h6", "div", "span", "p")
        and t.get_text(strip=True).lower() == "new series"
    )
    if not heading:
        return []

    box = heading
    while box and not box.select('a[href*="/series/"]'):
        box = box.parent

    slugs = []
    if box:
        for a in box.select('a[href*="/series/"]'):
            m = SERIES_RE.search(a["href"])
            if m and m.group(1) not in slugs:
                slugs.append(m.group(1))
    return slugs

def get_alt_name(soup, title):
    """Alternative name = the first line of text right after the <h1> title."""
    h1 = soup.find("h1")
    if not h1:
        return ""
    el = h1.find_next(lambda t: t.name and not t.find(True) and t.get_text(strip=True))
    t = el.get_text(" ", strip=True) if el else ""
    if (
        not t
        or len(t) > 150
        or t.lower() == (title or "").lower()
        or re.fullmatch(r"[\d.]+", t)          # a rating like "5.0"
        or "rating" in t.lower()
        or t.lower() == "synopsis"
    ):
        return ""
    return t

def get_details(slug):
    url = f"{BASE}/series/{slug}"
    soup = get_soup(url)
    page_text = soup.get_text("\n", strip=True)

    def meta(prop):
        tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
        return tag["content"].strip() if tag and tag.get("content") else ""

    def link_texts(fragment):
        seen, out = set(), []
        for a in soup.select(f'a[href*="{fragment}"]'):
            t = a.get_text(strip=True)
            if t and t.lower() not in seen:
                seen.add(t.lower())
                out.append(t)
        return out

    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else meta("og:title")

    synopsis = ""
    heading = soup.find(
        lambda t: t.name in ("h2", "h3", "h4") and t.get_text(strip=True).lower() == "synopsis"
    )
    if heading:
        parts = []
        for sib in heading.find_next_siblings():
            if sib.name in ("h2", "h3", "h4"):
                break
            parts.append(sib.get_text(" ", strip=True))
        synopsis = " ".join(parts).replace("See more", "").strip()
    if not synopsis:
        synopsis = meta("og:description")

    m = re.search(r"Total Chapters\s*(\d+)", page_text)
    chapters = m.group(1) if m else "N/A"

    status = link_texts("browse?status=")
    mtype = link_texts("browse?type=")
    return {
        "title": title,
        "alt": get_alt_name(soup, title),
        "url": url,
        "image": meta("og:image"),
        "synopsis": synopsis,
        "genres": link_texts("browse?genre="),
        "status": status[0] if status else "Unknown",
        "type": mtype[0] if mtype else "",
        "chapters": chapters,
    }


# ---------- Telegram ----------
def build_caption(d):
    esc = html.escape
    head = f"<b>{esc(d['title'])}</b>\n"
    info = (
        "🆕 New series\n"
        f"📖 <b>Chapters:</b> {d['chapters']}\n"
        f"📌 <b>Status:</b> {esc(d['status'])}\n"
    )
    if d["type"]:
        info += f"📚 <b>Type:</b> {esc(d['type'])}\n"
    if d["genres"]:
        info += f"🏷 <b>Genres:</b> {esc(', '.join(d['genres']))}\n"
    if d.get("alt"):
        info += f"🔤 <b>Alternative name:</b> {esc(d['alt'])}\n"
    info += "🌐 <b>Source:</b> QIMANGA\n"

    room = 1024 - len(head) - len(info) - 4
    syn = d["synopsis"]
    while syn and len(html.escape(syn)) > room:
        syn = syn[: max(0, len(syn) - 25)].rstrip() + "…"
        if len(syn) < 30:
            syn = ""
            break
    body = f"\n{esc(syn)}" if syn else ""
    return head + info + body


def send(d):
    caption = build_caption(d)
    markup = json.dumps({"inline_keyboard": [[{"text": "📖 Read now", "url": d["url"]}]]})
    r = None
    if d["image"]:
        r = requests.post(
            TG + "sendPhoto",
            data={
                "chat_id": CHAT_ID, "photo": d["image"], "caption": caption,
                "parse_mode": "HTML", "reply_markup": markup,
            },
            timeout=15,
        )
    if r is None or not r.ok:
        r = requests.post(
            TG + "sendMessage",
            data={
                "chat_id": CHAT_ID, "text": caption, "parse_mode": "HTML",
                "reply_markup": markup, "disable_web_page_preview": True,
            },
            timeout=15,
        )
    r.raise_for_status()


# ---------- One run ----------
def run():
    slugs = get_new_series()
    if not slugs:
        return "no 'New Series' items found"

    seen = load_seen()
    first_run = len(seen) == 0
    sent = 0

    for slug in slugs:
        if slug in seen:
            continue
        if first_run:  # seed only, don't spam
            mark_seen(slug)
            continue
        if sent >= MAX_PER_RUN:
            break
        try:
            d = get_details(slug)
            if is_duplicate(d):  # same series, renamed -> remember slug, don't resend
                print(f"Skipping duplicate: {slug}")
                mark_seen(slug, d)
                continue
            send(d)
        except Exception as e:
            print(f"Failed to send {slug}: {e}")
            continue  # not marked as seen -> retried next run
        mark_seen(slug, d)
        sent += 1

    return f"ok: sent={sent}, first_run={first_run}, carousel={len(slugs)}"


# ---------- Telegram commands (/start /help /status) ----------
def tg_reply(chat_id, text):
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


# ---------- Routing (Vercel runs this one file for every URL) ----------
class handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body if isinstance(body, bytes) else body.encode())

    def _path(self):
        return self.path.split("?")[0].rstrip("/") or "/"

    def do_GET(self):
        path = self._path()
        if path == "/api/webhook":
            return self._send(200, b"webhook alive")
        if path not in ("/api/main", "/api/cron"):
            return self._send(404, b"not found")

        # --- cron job ---
        if CRON_SECRET and self.headers.get("Authorization") != f"Bearer {CRON_SECRET}":
            return self._send(401, b"unauthorized")
        try:
            body, code = run(), 200
            if body.startswith("ok"):
                record_status(result=body)
            else:  # e.g. "no 'New Series' items found" = site layout changed
                record_status(error=body)
        except Exception as e:
            print("Run failed:", e)
            body, code = f"error: {e}", 500
            record_status(error=str(e)[:300])
        self._send(code, body)

    def do_POST(self):
        if self._path() != "/api/webhook":
            return self._send(404, b"not found")
        if WEBHOOK_SECRET and self.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
            return self._send(401, b"unauthorized")
        try:
            length = int(self.headers.get("Content-Length", 0))
            update = json.loads(self.rfile.read(length) or b"{}")
            msg = update.get("message") or {}
            text = (msg.get("text") or "").strip()
            chat_id = (msg.get("chat") or {}).get("id")
            if chat_id and text.startswith("/"):
                cmd = text.split()[0].split("@")[0].lower()
                if cmd in ("/start", "/help"):
                    tg_reply(chat_id, HELP)
                elif cmd == "/status":
                    tg_reply(chat_id, status_text())
        except Exception as e:
            print("webhook error:", e)
        self._send(200, b"ok")  # always 200 so Telegram doesn't retry endlessly