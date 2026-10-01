import html
import os
import re
from http.server import BaseHTTPRequestHandler

import requests
from bs4 import BeautifulSoup
from supabase import create_client

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


# ---------- Supabase ----------
def load_seen():
    res = supabase.table("new_series").select("slug").execute()
    return {row["slug"] for row in res.data}


def mark_seen(slug):
    supabase.table("new_series").upsert({"slug": slug}).execute()


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
    info = f"🆕 New series\n📖 Chapters: {d['chapters']}\n📌 Status: {esc(d['status'])}\n"
    if d["type"]:
        info += f"📚 Type: {esc(d['type'])}\n"
    if d["genres"]:
        info += f"🏷 Genres: {esc(', '.join(d['genres']))}\n"
    tail = f'\n<a href="{d["url"]}">Read now</a>'

    room = 1024 - len(head) - len(info) - len(tail) - 4
    syn = d["synopsis"]
    while syn and len(html.escape(syn)) > room:
        syn = syn[: max(0, len(syn) - 25)].rstrip() + "…"
        if len(syn) < 30:
            syn = ""
            break
    body = f"\n{esc(syn)}\n" if syn else ""
    return head + info + body + tail


def send(d):
    caption = build_caption(d)
    r = None
    if d["image"]:
        r = requests.post(
            TG + "sendPhoto",
            data={"chat_id": CHAT_ID, "photo": d["image"], "caption": caption, "parse_mode": "HTML"},
            timeout=15,
        )
    if r is None or not r.ok:
        r = requests.post(
            TG + "sendMessage",
            data={"chat_id": CHAT_ID, "text": caption, "parse_mode": "HTML"},
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
            send(get_details(slug))
        except Exception as e:
            print(f"Failed to send {slug}: {e}")
            continue  # not marked as seen -> retried next run
        mark_seen(slug)
        sent += 1

    return f"ok: sent={sent}, first_run={first_run}, carousel={len(slugs)}"


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if CRON_SECRET and self.headers.get("Authorization") != f"Bearer {CRON_SECRET}":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"unauthorized")
            return
        try:
            body, code = run(), 200
        except Exception as e:
            print("Run failed:", e)
            body, code = f"error: {e}", 500
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body.encode())
