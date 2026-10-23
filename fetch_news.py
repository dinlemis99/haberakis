#!/usr/bin/env python3
"""GitHub Actions icin: Google News RSS'ten haberleri ceker, her haberin asil
sayfasindan kapak resmini (og:image) bulur ve news/<kategori>.json yazar.
Sadece standart kutuphane kullanir.

- Once son 2 saatin haberleri alinir; azsa 6 saate, 24 saate, 72 saate genisler.
- Google'in sifreli haber linki cozulur, asil haber sayfasi acilip resim okunur.
- Resim bulunamazsa haber resimsiz kalir (sitede kategori simgesi gorunur).
- Onceki yayindan bilinen resimler tekrar aranmaz (hizli ve Google'i yormaz).
- Hic haber alinamazsa is hata verir; boylece siteye bos veri yayinlanmaz.
"""
import base64
import concurrent.futures as cf
import email.utils
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
MAX_ITEMS = 20
MIN_ITEMS = 6
WINDOWS = ("2h", "6h", "24h", "72h")
WORKERS = 6
ENRICH_BUDGET_SEC = 480  # resim arama icin toplam sure siniri

QUERIES = {
    "gundem": "türkiye gündem son dakika",
    "ekonomi": "türkiye ekonomi borsa dolar",
    "spor": "türkiye spor futbol",
    "teknoloji": "teknoloji yapay zeka",
    "saglik": "sağlık türkiye",
    "dunya": "dünya haberleri son dakika",
    "kultur": "kültür sanat sinema",
    "bilim": "bilim uzay araştırma",
}


def http_get(url, limit=8_000_000, timeout=20, accept="*/*"):
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": accept, "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.5"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(limit)


def parse_xml(data):
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        text = data.decode("utf-8", errors="replace")
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]", "", text)
        text = re.sub(r"&(?!(amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)", "&amp;", text)
        text = re.sub(r"^<\?xml[^>]*\?>", "", text.strip())
        return ET.fromstring(text)


def parse_date(text):
    text = (text or "").strip()
    if not text:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone(timedelta(hours=3)))
    dt = dt.astimezone(timezone.utc)
    now = datetime.now(timezone.utc)
    return now if dt > now + timedelta(minutes=10) else dt


def clean_text(raw):
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw or ""))
    return re.sub(r"\s+", " ", text).strip()


def google_items(cat, window):
    q = urllib.parse.quote(f"{QUERIES[cat]} when:{window}")
    url = f"https://news.google.com/rss/search?q={q}&hl=tr&gl=TR&ceid=TR:tr"
    root = parse_xml(http_get(url, accept="application/rss+xml, application/xml, */*"))
    items = []
    for item in root.iter("item"):
        title = clean_text(item.findtext("title"))
        link = (item.findtext("link") or "").strip()
        dt = parse_date(item.findtext("pubDate"))
        if not title or not link or dt is None:
            continue
        source = ""
        src_el = item.find("source")
        if src_el is not None and (src_el.text or "").strip():
            source = src_el.text.strip()
        if source and title.endswith(" - " + source):
            title = title[: -len(" - " + source)]
        items.append({"title": title, "link": link, "source": source or "Google Haberler", "dt": dt, "desc": "", "img": ""})
    return items


def pick_recent(items):
    """Pencereyi genislete, sonra kaynaklari sirayla karistirip en fazla MAX_ITEMS sec."""
    now = datetime.now(timezone.utc)
    chosen = []
    for w in WINDOWS:
        hours = int(w[:-1])
        chosen = [i for i in items if now - i["dt"] <= timedelta(hours=hours)]
        if len(chosen) >= MIN_ITEMS:
            break
    queues = {}
    for it in sorted(chosen, key=lambda i: i["dt"], reverse=True):
        queues.setdefault(it["source"], []).append(it)
    mixed = []
    while queues and len(mixed) < MAX_ITEMS:
        for name in list(queues):
            mixed.append(queues[name].pop(0))
            if not queues[name]:
                del queues[name]
            if len(mixed) >= MAX_ITEMS:
                break
    mixed.sort(key=lambda i: i["dt"], reverse=True)
    return mixed


def build_category(cat):
    items = []
    for w in ("2h", "6h", "24h"):
        try:
            items = google_items(cat, w)
        except Exception as e:
            print(f"  google {w}: hata {type(e).__name__}: {str(e)[:70]}")
            continue
        print(f"  google {w}: {len(items)} haber")
        if len(items) >= 12:
            break
    seen, unique = set(), []
    for it in sorted(items, key=lambda i: i["dt"], reverse=True):
        key = re.sub(r"\W+", "", it["title"].lower())[:60]
        if key not in seen:
            seen.add(key)
            unique.append(it)
    return pick_recent(unique)


# ---------- Google linkini coz, haber sayfasindan resim bul ----------

def decode_google_url(link):
    parts = urllib.parse.urlparse(link)
    if parts.netloc != "news.google.com":
        return link
    segs = [s for s in parts.path.split("/") if s]
    if len(segs) < 2 or segs[-2] not in ("articles", "read"):
        return ""
    b64 = segs[-1]
    # Eski bicim: adres dogrudan base64 icinde.
    try:
        raw = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))
        m = re.search(rb"https?://[\x21-\x7e]+", raw)
        if m:
            return m.group(0).decode()
    except Exception:
        pass
    # Yeni bicim: Google'a sorulur.
    page = http_get(f"https://news.google.com/articles/{b64}?hl=en-US&gl=US&ceid=US:en", limit=600_000, timeout=15).decode("utf-8", "replace")
    sg = re.search(r'data-n-a-sg="([^"]+)"', page)
    ts = re.search(r'data-n-a-ts="([^"]+)"', page)
    if not sg or not ts:
        return ""
    inner = (
        '["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],'
        f'"X","X",1,[1,1,1],1,1,null,0,0,null,0],"{b64}",{ts.group(1)},"{sg.group(1)}"]'
    )
    body = "f.req=" + urllib.parse.quote(json.dumps([[["Fbv4je", inner]]]))
    req = urllib.request.Request(
        "https://news.google.com/_/DotsSplashUi/data/batchexecute",
        data=body.encode(),
        headers={"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        text = resp.read(1_000_000).decode("utf-8", "replace")
    outer = json.loads(text.split("\n\n")[1])
    return json.loads(outer[0][2])[1]


def og_image(page_url):
    data = http_get(page_url, limit=400_000, timeout=12, accept="text/html,*/*").decode("utf-8", "replace")
    for pat in (
        r'<meta[^>]+(?:property|name)=["\'](?:og:image(?::secure_url)?|twitter:image(?::src)?)["\'][^>]*?content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*?(?:property|name)=["\'](?:og:image|twitter:image)["\']',
    ):
        m = re.search(pat, data, re.I)
        if m:
            url = urllib.parse.urljoin(page_url, html.unescape(m.group(1)).strip())
            if url.startswith("http://"):
                url = "https://" + url[7:]
            if url.startswith("https://"):
                return url
    return ""


def find_article_image(link):
    try:
        real = decode_google_url(link)
        if not real or "news.google.com" in real:
            return "", "cozulemedi"
        img = og_image(real)
        return img, "" if img else "resim yok"
    except Exception as e:
        return "", type(e).__name__


def load_previous(cat):
    """Onceki yayindaki resimleri oku: ayni haber icin tekrar arama yapma."""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" not in repo:
        return {}
    owner, name = repo.split("/", 1)
    try:
        data = json.loads(http_get(f"https://{owner}.github.io/{name}/news/{cat}.json", timeout=10))
        return {i["link"]: i["img"] for i in data.get("items", []) if i.get("img")}
    except Exception:
        return {}


def main():
    os.makedirs("news", exist_ok=True)
    chosen = {}
    for cat in QUERIES:
        print(f"[{cat}]")
        chosen[cat] = build_category(cat)
        prev = load_previous(cat)
        for it in chosen[cat]:
            it["img"] = prev.get(it["link"], "")
        print(f"  => {len(chosen[cat])} haber secildi, {sum(1 for i in chosen[cat] if i['img'])} resim onceden biliniyor")

    todo = [it for cat in chosen for it in chosen[cat] if not it["img"]]
    print(f"\nResim araniyor: {len(todo)} haber")
    stats = {}
    start = time.monotonic()
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futs = {pool.submit(find_article_image, it["link"]): it for it in todo}
        try:
            for fut in cf.as_completed(futs, timeout=ENRICH_BUDGET_SEC):
                it = futs[fut]
                img, why = fut.result()
                it["img"] = img
                key = "bulundu" if img else why
                stats[key] = stats.get(key, 0) + 1
        except cf.TimeoutError:
            print("Sure siniri doldu, kalan haberler resimsiz birakildi.")
            for f in futs:
                f.cancel()
    print(f"Resim arama {time.monotonic() - start:.0f} sn surdu: {stats}")

    now = datetime.now(timezone.utc)
    total = 0
    for cat, items in chosen.items():
        # Vitrin haberi resimli olsun: ilk 5 icinde resimli varsa en uste al.
        for idx, it in enumerate(items[:5]):
            if it["img"]:
                items.insert(0, items.pop(idx))
                break
        total += len(items)
        out = {
            "updated": now.isoformat(),
            "items": [
                {"title": i["title"], "link": i["link"], "source": i["source"],
                 "time": i["dt"].isoformat(), "desc": i["desc"], "img": i["img"]}
                for i in items
            ],
        }
        with open(os.path.join("news", f"{cat}.json"), "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
        print(f"[{cat}] {len(items)} haber, {sum(1 for i in items if i['img'])} resimli")
    if total == 0:
        print("Hicbir kategoriden haber alinamadi.")
        sys.exit(1)


if __name__ == "__main__":
    main()
