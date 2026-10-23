#!/usr/bin/env python3
"""GitHub Actions icin: haber sitelerinin RSS'lerinden resimli haberleri cekip
news/<kategori>.json dosyalarina yazar. Sadece standart kutuphane kullanir.

- Her kategori icin birkac kaynak denenir, calismayan kaynak atlanir.
- Once son 2 saatin haberleri alinir; azsa 6 saate, 24 saate, 72 saate genisler.
- Hicbir kaynaktan haber gelmezse Google News'e (resimsiz) dusulur.
- Hic haber alinamazsa is hata verir; boylece siteye bos veri yayinlanmaz.
"""
import email.utils
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
NS = {
    "media": "http://search.yahoo.com/mrss/",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "atom": "http://www.w3.org/2005/Atom",
}
MAX_ITEMS = 20
MIN_ITEMS = 6
WINDOWS_HOURS = (2, 6, 24, 72)

NTV = "https://www.ntv.com.tr/{}.rss"
HUR = "https://www.hurriyet.com.tr/rss/{}"
SAB = "https://www.sabah.com.tr/rss/{}.xml"
TRT = "https://www.trthaber.com/{}_articles.rss"
CNN = "https://www.cnnturk.com/feed/rss/{}/news"

FEEDS = {
    "gundem": [
        ("NTV", NTV.format("gundem")),
        ("Hürriyet", HUR.format("gundem")),
        ("Sabah", SAB.format("gundem")),
        ("TRT Haber", TRT.format("gundem")),
        ("CNN Türk", CNN.format("turkiye")),
    ],
    "ekonomi": [
        ("NTV", NTV.format("ekonomi")),
        ("Hürriyet", HUR.format("ekonomi")),
        ("Sabah", SAB.format("ekonomi")),
        ("TRT Haber", TRT.format("ekonomi")),
        ("CNN Türk", CNN.format("ekonomi")),
    ],
    "spor": [
        ("NTV", NTV.format("spor")),
        ("Hürriyet", HUR.format("spor")),
        ("Sabah", SAB.format("spor")),
        ("TRT Haber", TRT.format("spor")),
        ("CNN Türk", CNN.format("spor")),
    ],
    "teknoloji": [
        ("NTV", NTV.format("teknoloji")),
        ("Hürriyet", HUR.format("teknoloji")),
        ("Sabah", SAB.format("teknoloji")),
        ("TRT Haber", TRT.format("bilim_teknoloji")),
        ("CNN Türk", CNN.format("bilim-teknoloji")),
    ],
    "saglik": [
        ("NTV", NTV.format("saglik")),
        ("Hürriyet", HUR.format("saglik")),
        ("Sabah", SAB.format("saglik")),
        ("TRT Haber", TRT.format("saglik")),
        ("CNN Türk", CNN.format("saglik")),
    ],
    "dunya": [
        ("NTV", NTV.format("dunya")),
        ("Hürriyet", HUR.format("dunya")),
        ("Sabah", SAB.format("dunya")),
        ("TRT Haber", TRT.format("dunya")),
        ("CNN Türk", CNN.format("dunya")),
    ],
    "kultur": [
        ("NTV", NTV.format("sanat")),
        ("Hürriyet", HUR.format("kultur-sanat")),
        ("Sabah", SAB.format("kultur-sanat")),
        ("TRT Haber", TRT.format("kultur_sanat")),
        ("CNN Türk", CNN.format("kultur-sanat")),
    ],
    "bilim": [
        ("TRT Haber", TRT.format("bilim_teknoloji")),
        ("CNN Türk", CNN.format("bilim-teknoloji")),
        ("Evrim Ağacı", "https://evrimagaci.org/rss.php"),
        ("NTV", NTV.format("teknoloji")),
    ],
}

# Hicbir kaynak calismazsa kullanilacak yedek (resimsiz).
GOOGLE_QUERIES = {
    "gundem": "türkiye gündem son dakika",
    "ekonomi": "türkiye ekonomi borsa dolar",
    "spor": "türkiye spor futbol",
    "teknoloji": "teknoloji yapay zeka",
    "saglik": "sağlık türkiye",
    "dunya": "dünya haberleri son dakika",
    "kultur": "kültür sanat sinema",
    "bilim": "bilim uzay araştırma",
}


def http_get(url):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
            "Accept-Language": "tr-TR,tr;q=0.9",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read(8_000_000)


def parse_xml(data):
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        text = data.decode("utf-8", errors="replace")
        # Cıplak & ve tanimsiz varliklari (&nbsp; gibi) duz metne cevir.
        text = re.sub(r"&(?!(amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)", "&amp;", text)
        text = re.sub(r"^<\?xml[^>]*\?>", "", text.strip())
        return ET.fromstring(text)


def fix_url(url):
    url = (url or "").strip()
    if url.startswith("//"):
        url = "https:" + url
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    return url if url.startswith("https://") else ""


def find_image(item):
    for enc in item.findall("enclosure"):
        url = enc.get("url", "")
        if (enc.get("type") or "").startswith("image") or re.search(r"\.(jpe?g|png|webp)", url, re.I):
            if fix_url(url):
                return fix_url(url)
    for tag in ("media:content", "media:thumbnail"):
        for el in item.findall(tag, NS):
            url = el.get("url", "")
            kind = (el.get("type") or el.get("medium") or "image")
            if kind.startswith("image") and fix_url(url):
                return fix_url(url)
    for tag in ("image", "img", "imageUrl", "ipimage", "picture"):
        el = item.find(tag)
        if el is not None and fix_url(el.text or ""):
            return fix_url(el.text)
    blobs = (item.findtext("content:encoded", namespaces=NS), item.findtext("description"))
    for blob in blobs:
        m = re.search(r"""<img[^>]+src=["']([^"']+)""", blob or "", re.I)
        if m and fix_url(html.unescape(m.group(1))):
            return fix_url(html.unescape(m.group(1)))
    return ""


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
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def clean_text(raw, limit=None):
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] if limit else text


def parse_feed(data, default_source, strip_source_suffix=False):
    root = parse_xml(data)
    items = []
    for item in root.iter("item"):
        title = clean_text(item.findtext("title"))
        link = (item.findtext("link") or "").strip()
        if not link:
            atom = item.find("atom:link", NS)
            link = atom.get("href", "") if atom is not None else ""
        dt = parse_date(item.findtext("pubDate") or item.findtext("{http://purl.org/dc/elements/1.1/}date"))
        if not title or not link or dt is None:
            continue
        source = default_source
        src_el = item.find("source")
        if src_el is not None and (src_el.text or "").strip():
            source = src_el.text.strip()
        if strip_source_suffix and source and title.endswith(" - " + source):
            title = title[: -len(" - " + source)]
        desc = clean_text(item.findtext("description"), 300)
        if strip_source_suffix:
            desc = ""  # Google News ozeti sadece baslik tekrari
        items.append(
            {
                "title": title,
                "link": link,
                "source": source,
                "dt": dt,
                "desc": desc,
                "img": find_image(item),
            }
        )
    return items


def google_fallback(cat):
    found = []
    for window in ("2h", "6h", "24h"):
        q = urllib.parse.quote(f"{GOOGLE_QUERIES[cat]} when:{window}")
        url = f"https://news.google.com/rss/search?q={q}&hl=tr&gl=TR&ceid=TR:tr"
        try:
            found = parse_feed(http_get(url), "Google Haberler", strip_source_suffix=True)
        except Exception as e:
            print(f"  google {window}: hata {e}")
            continue
        if len(found) >= 3:
            break
    return found


def pick_recent(items, now):
    chosen = []
    for hours in WINDOWS_HOURS:
        chosen = [i for i in items if now - i["dt"] <= timedelta(hours=hours)]
        if len(chosen) >= MIN_ITEMS:
            break
    return chosen[:MAX_ITEMS]


def build_category(cat, now):
    items, worked = [], []
    for name, url in FEEDS[cat]:
        try:
            got = parse_feed(http_get(url), name)
        except Exception as e:
            print(f"  {name}: atlandi ({type(e).__name__}: {str(e)[:70]})")
            continue
        print(f"  {name}: {len(got)} haber, {sum(1 for g in got if g['img'])} resimli")
        if got:
            worked.append(name)
        items.extend(got)

    if len(items) < 3:
        print("  kaynaklar yetersiz, Google News yedegine geciliyor")
        items.extend(google_fallback(cat))

    items.sort(key=lambda i: i["dt"], reverse=True)
    seen, unique = set(), []
    for it in items:
        key = re.sub(r"\W+", "", it["title"].lower())[:60]
        if key in seen:
            continue
        seen.add(key)
        unique.append(it)

    chosen = pick_recent(unique, now)
    # Vitrin haberi resimli olsun: ilk 5 icinde resimli varsa en uste al.
    for idx, it in enumerate(chosen[:5]):
        if it["img"]:
            chosen.insert(0, chosen.pop(idx))
            break
    return chosen


def main():
    os.makedirs("news", exist_ok=True)
    now = datetime.now(timezone.utc)
    total = 0
    for cat in FEEDS:
        print(f"[{cat}]")
        items = build_category(cat, now)
        total += len(items)
        out = {
            "updated": now.isoformat(),
            "items": [
                {
                    "title": i["title"],
                    "link": i["link"],
                    "source": i["source"],
                    "time": i["dt"].isoformat(),
                    "desc": i["desc"],
                    "img": i["img"],
                }
                for i in items
            ],
        }
        with open(os.path.join("news", f"{cat}.json"), "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
        print(f"  => {len(items)} haber yazildi ({sum(1 for i in items if i['img'])} resimli)")
    if total == 0:
        print("Hicbir kategoriden haber alinamadi.")
        sys.exit(1)


if __name__ == "__main__":
    main()
