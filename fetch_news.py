#!/usr/bin/env python3
"""GitHub Actions icin: Google News RSS'ten haberleri ceker, her haberin asil
sayfasindan kapak resmini (og:image) bulur ve news/<kategori>.json yazar.
Sadece standart kutuphane kullanir.

- Once son 2 saatin haberleri alinir; azsa 6 saate, 24 saate, 72 saate genisler.
- Google'in sifreli haber linki cozulur, asil haber sayfasi acilip resim, ozet ve tam metin okunur (news/a/<id>.json).
- news/tumu.json: tum kategorilerden son 24 saatin dengeli, en yeni ustte karisik akisi.
- Resim bulunamazsa haber resimsiz kalir (sitede kategori simgesi gorunur).
- Onceki yayindan bilinen resimler tekrar aranmaz (hizli ve Google'i yormaz).
- Hic haber alinamazsa is hata verir; boylece siteye bos veri yayinlanmaz.
"""
import base64
import hashlib
import concurrent.futures as cf
import email.utils
import html
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
# Sifre: GitHub "Secrets" icinde HABER_SIFRE olarak saklanir. Doluysa tum haber
# dosyalari AES-GCM ile sifrelenir; uygulama acilista sifreyi sorar.
PASSWORD = unicodedata.normalize("NFC", os.environ.get("HABER_SIFRE", "")).strip()
PBKDF2_ITER = 200_000
_keys = {}
MAX_ITEMS = 20
MIN_ITEMS = 6
PER_CAT_ALL = 14   # "Tumu" akisinda her kategoriden en fazla bu kadar haber (son 24 saat)
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
        items.append({"title": title, "link": link, "source": source or "Google Haberler", "dt": dt, "desc": "", "img": "", "url": ""})
    return items


def pick_recent(items, hours_steps, min_items, max_items):
    """Pencereyi genislet, sonra kaynaklari sirayla karistirip en fazla max_items sec."""
    now = datetime.now(timezone.utc)
    chosen = []
    for hours in hours_steps:
        chosen = [i for i in items if now - i["dt"] <= timedelta(hours=hours)]
        if len(chosen) >= min_items:
            break
    queues = {}
    for it in sorted(chosen, key=lambda i: i["dt"], reverse=True):
        queues.setdefault(it["source"], []).append(it)
    mixed = []
    while queues and len(mixed) < max_items:
        for name in list(queues):
            mixed.append(queues[name].pop(0))
            if not queues[name]:
                del queues[name]
            if len(mixed) >= max_items:
                break
    mixed.sort(key=lambda i: i["dt"], reverse=True)
    return mixed


def title_key(t):
    return re.sub(r"\W+", "", t.lower())[:60]


def build_pool(cat):
    """Kategorinin son 24 saatlik haber havuzu (az ise 72 saate genisler)."""
    items = []
    for w in ("24h", "72h"):
        try:
            items = google_items(cat, w)
        except Exception as e:
            print(f"  google {w}: hata {type(e).__name__}: {str(e)[:70]}")
            continue
        print(f"  google {w}: {len(items)} haber")
        if len(items) >= MIN_ITEMS:
            break
    seen, unique = set(), []
    for it in sorted(items, key=lambda i: i["dt"], reverse=True):
        key = title_key(it["title"])
        if key not in seen:
            seen.add(key)
            it["cat"] = cat
            unique.append(it)
    return unique


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
    page, last = "", ""
    for u in (f"https://news.google.com/rss/articles/{b64}?hl=tr&gl=TR&ceid=TR:tr",
              f"https://news.google.com/articles/{b64}?hl=en-US&gl=US&ceid=US:en"):
        req = urllib.request.Request(u, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
                                                 "Cookie": "CONSENT=YES+cb; SOCS=CAI"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                page = resp.read(800_000).decode("utf-8", "replace")
        except Exception as e:
            last = f"sayfa-{type(e).__name__}"
            continue
        sg = re.search(r'data-n-a-sg=["\']([^"\']+)', page)
        ts = re.search(r'data-n-a-ts=["\']([^"\']+)', page)
        if sg and ts:
            break
        last = "imza-yok"
        note_failure(u, page)
    else:
        raise ValueError(last or "imza-yok")
    inner = (
        '["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],'
        f'"X","X",1,[1,1,1],1,1,null,0,0,null,0],"{b64}",{ts.group(1)},"{sg.group(1)}"]'
    )
    body = "f.req=" + urllib.parse.quote(json.dumps([[["Fbv4je", inner]]]))
    req = urllib.request.Request(
        "https://news.google.com/_/DotsSplashUi/data/batchexecute",
        data=body.encode(),
        headers={"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                 "Cookie": "CONSENT=YES+cb; SOCS=CAI"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        text = resp.read(1_000_000).decode("utf-8", "replace")
    try:
        outer = json.loads(text.split("\n\n")[1])
        return json.loads(outer[0][2])[1]
    except Exception:
        note_failure("batchexecute", text)
        raise ValueError("yanit-cozulemedi")


_notes = []


def note_failure(where, text):
    """Ilk birkac hatada Google'in ne dondurdugunu loga yaz (teshis icin)."""
    if len(_notes) < 3:
        _notes.append(where)
        snippet = re.sub(r"\s+", " ", text[:400])
        print(f"  [teshis] {where}: uzunluk={len(text)} ornek={snippet!r}")


def meta_content(page, names):
    for n in names:
        for pat in (
            r'<meta[^>]+(?:property|name)=["\']' + n + r'["\'][^>]*?content=["\']([^"\']*)',
            r'<meta[^>]+content=["\']([^"\']*)["\'][^>]*?(?:property|name)=["\']' + n + r'["\']',
        ):
            m = re.search(pat, page, re.I)
            if m and m.group(1).strip():
                return html.unescape(m.group(1)).strip()
    return ""


def shorten(text, limit=480):
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return cut[: end + 1] if end > limit * 0.5 else cut.rsplit(" ", 1)[0] + "…"


def hid(text):
    """index.html'deki hid() ile ayni sonucu verir (haber kimligi)."""
    h = 0
    data = text.encode("utf-16-le")
    for i in range(0, len(data), 2):
        unit = data[i] | (data[i + 1] << 8)
        h = (h * 31 + unit) & 0xFFFFFFFF
    digits, n = "0123456789abcdefghijklmnopqrstuvwxyz", h
    out = ""
    while n:
        out = digits[n % 36] + out
        n //= 36
    return "h" + (out or "0")


BOILER = re.compile(
    r"abone ol|tüm hakları|çerez|cookie|©|bültene|haberi paylaş|yorum yap|kvkk|gizlilik|"
    r"whatsapp|telegram|facebook|twitter|instagram|reklam|sosyal medya hesab|google news|"
    r"devamını oku|ilgili haber|tıklayın|uygulamamızı", re.I)


def article_body(page):
    """Haber sayfasindan paragraf listesi (yaklasik; reklam/menu atilir)."""
    page = re.sub(r"(?is)<(script|style|noscript|iframe|nav|header|footer|aside|form|figure|button)\b[^>]*>.*?</\1>", " ", page)
    arts = re.findall(r"(?is)<article\b[^>]*>(.*?)</article>", page)
    scope = max(arts, key=len) if arts else page
    paras, seen, total = [], set(), 0
    for raw in re.findall(r"(?is)<p\b[^>]*>(.*?)</p>", scope):
        p = clean_text(raw)
        if len(p) < 40 or p in seen:
            continue
        if len(p) < 220 and BOILER.search(p):
            continue
        seen.add(p)
        paras.append(p)
        total += len(p)
        if total > 16000 or len(paras) >= 60:
            break
    return paras if len(paras) >= 2 and total >= 300 else []


def page_info(page_url):
    """Haber sayfasindan kapak resmi ve ozet metni."""
    page = http_get(page_url, limit=500_000, timeout=12, accept="text/html,*/*").decode("utf-8", "replace")
    img = meta_content(page, ["og:image:secure_url", "og:image", "twitter:image", "twitter:image:src"])
    if img:
        img = urllib.parse.urljoin(page_url, img)
        if img.startswith("http://"):
            img = "https://" + img[7:]
        if not img.startswith("https://"):
            img = ""
    desc = clean_text(meta_content(page, ["og:description", "twitter:description", "description"]))
    if len(desc) < 120:  # kisa ise sayfadaki ilk paragraflari ekle
        body = re.sub(r"(?is)<(script|style|nav|header|footer|aside|form)[^>]*>.*?</\1>", " ", page)
        paras = [clean_text(p) for p in re.findall(r"(?is)<p[^>]*>(.*?)</p>", body)]
        paras = [p for p in paras if len(p) > 70 and p != desc]
        desc = (desc + " " + " ".join(paras[:3])).strip()
    return img, shorten(desc), article_body(page)


def find_article_info(link):
    try:
        real = decode_google_url(link)
        if not real or "news.google.com" in real:
            return {"why": "cozulemedi"}
        img, desc, body = page_info(real)
        return {"img": img, "desc": desc, "url": real, "body": body, "why": "" if img else "resim yok"}
    except Exception as e:
        return {"why": f"{type(e).__name__}:{str(e)[:30]}"}


def _aesgcm():
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return AESGCM


def derive_key(salt):
    if salt not in _keys:
        _keys[salt] = hashlib.pbkdf2_hmac("sha256", PASSWORD.encode("utf-8"), salt, PBKDF2_ITER, 32)
    return _keys[salt]


def prev_json(url):
    """Onceki yayindaki dosyayi indir; sifreliyse coz."""
    d = json.loads(http_get(url, timeout=10))
    if isinstance(d, dict) and d.get("enc") == 1:
        if not PASSWORD:
            raise ValueError("sifreli ama sifre yok")
        salt = base64.b64decode(d["salt"])
        raw = _aesgcm()(derive_key(salt)).decrypt(base64.b64decode(d["iv"]), base64.b64decode(d["ct"]), None)
        return json.loads(raw)
    return d


def encrypt_all():
    """news/ altindaki tum .json dosyalarini sifrele (sifre varsa)."""
    if not PASSWORD:
        print("UYARI: HABER_SIFRE tanimli degil, haber dosyalari SIFRESIZ yayinlanacak.")
        return
    try:
        AESGCM = _aesgcm()
    except ImportError:
        print("HATA: 'cryptography' paketi yok; sifresiz yayin yapmamak icin is durduruldu.")
        sys.exit(1)
    salt = os.urandom(16)
    aes = AESGCM(derive_key(salt))
    count = 0
    for root, _, files in os.walk("news"):
        for fn in files:
            if not fn.endswith(".json"):
                continue
            path = os.path.join(root, fn)
            with open(path, "rb") as f:
                raw = f.read()
            iv = os.urandom(12)
            blob = {"enc": 1, "salt": base64.b64encode(salt).decode(), "iv": base64.b64encode(iv).decode(),
                    "ct": base64.b64encode(aes.encrypt(iv, raw, None)).decode()}
            with open(path, "w", encoding="utf-8") as f:
                json.dump(blob, f)
            count += 1
    print(f"{count} haber dosyasi sifrelendi.")


def load_previous():
    """Onceki yayindaki resim/ozetleri oku: ayni haber icin tekrar arama yapma."""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" not in repo:
        return {}
    owner, name = repo.split("/", 1)
    prev = {}
    for cat in list(QUERIES) + ["tumu"]:
        try:
            data = prev_json(f"https://{owner}.github.io/{name}/news/{cat}.json")
        except Exception:
            continue
        for i in data.get("items", []):
            if i.get("link") and (i.get("img") or i.get("desc")):
                prev[i["link"]] = {"img": i.get("img", ""), "desc": i.get("desc", ""), "url": i.get("url", "")}
    return prev


def prev_base():
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" not in repo:
        return ""
    owner, name = repo.split("/", 1)
    return f"https://{owner}.github.io/{name}"


def write_body(link, paras):
    os.makedirs(os.path.join("news", "a"), exist_ok=True)
    with open(os.path.join("news", "a", hid(link) + ".json"), "w", encoding="utf-8") as f:
        json.dump({"p": paras}, f, ensure_ascii=False)


def work(link, p):
    """Bir haber icin: onceki yayindaki bilgiyi (resim+ozet+metin) al; yoksa sayfadan cek."""
    base = prev_base()
    if p and p.get("img") and base:
        try:
            data = prev_json(f"{base}/news/a/{hid(link)}.json")
            if data.get("p"):
                write_body(link, data["p"])
                return {"img": p["img"], "desc": p.get("desc", ""), "url": p.get("url", ""), "body": data["p"], "why": "", "reused": True}
        except Exception:
            pass
    r = find_article_info(link)
    if r.get("body"):
        write_body(link, r["body"])
    return r


def to_json(items, now):
    return {
        "updated": now.isoformat(),
        "items": [
            {"title": i["title"], "link": i["link"], "url": i.get("url", ""), "source": i["source"],
             "time": i["dt"].isoformat(), "desc": i["desc"], "img": i["img"], "cat": i.get("cat", ""),
             "full": bool(i.get("full"))}
            for i in items
        ],
    }


def write_json(name, items, now):
    with open(os.path.join("news", f"{name}.json"), "w", encoding="utf-8") as f:
        json.dump(to_json(items, now), f, ensure_ascii=False)


def main():
    os.makedirs("news", exist_ok=True)
    prev = load_previous()
    print(f"Onceki yayindan {len(prev)} haber bilgisi okundu")

    cat_lists, all_lists, seen_all = {}, [], set()
    for cat in QUERIES:
        print(f"[{cat}]")
        pool = build_pool(cat)
        cat_lists[cat] = pick_recent(pool, (2, 6, 24, 72), MIN_ITEMS, MAX_ITEMS)
        part = []
        for it in pick_recent(pool, (24,), 1, PER_CAT_ALL * 2):
            key = title_key(it["title"])
            if key in seen_all or len(part) >= PER_CAT_ALL:
                continue
            seen_all.add(key)
            part.append(it)
        all_lists.extend(part)
        print(f"  => kategori listesi {len(cat_lists[cat])}, tumu akisina {len(part)} haber")
    all_lists.sort(key=lambda i: i["dt"], reverse=True)

    # Ayni haberin tum kopyalari (kategori + tumu) icin bir kez resim/ozet ara.
    by_link = {}
    for lst in list(cat_lists.values()) + [all_lists]:
        for it in lst:
            by_link.setdefault(it["link"], [])
            if not any(x is it for x in by_link[it["link"]]):
                by_link[it["link"]].append(it)
    todo = list(by_link)
    print(f"\nToplam {len(by_link)} farkli haber icin resim/ozet/metin araniyor")

    stats = {}
    start = time.monotonic()
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as pool_ex:
        futs = {pool_ex.submit(work, link, prev.get(link)): link for link in todo}
        try:
            for fut in cf.as_completed(futs, timeout=ENRICH_BUDGET_SEC):
                link = futs[fut]
                r = fut.result()
                for it in by_link[link]:
                    it["img"] = r.get("img", "")
                    it["desc"] = r.get("desc", "") or (prev.get(link, {}).get("desc", ""))
                    it["url"] = r.get("url", "")
                    it["full"] = bool(r.get("body"))
                if r.get("reused"):
                    stats["onceden"] = stats.get("onceden", 0) + 1
                else:
                    key = "bulundu" if r.get("img") else r.get("why", "?")
                    stats[key] = stats.get(key, 0) + 1
        except cf.TimeoutError:
            print("Sure siniri doldu, kalan haberler resimsiz birakildi.")
            for f in futs:
                f.cancel()
    print(f"Resim/ozet arama {time.monotonic() - start:.0f} sn surdu: {stats}")
    n_desc = sum(1 for g in by_link.values() if g[0].get("desc"))
    n_full = sum(1 for g in by_link.values() if g[0].get("full"))
    print(f"Ozeti olan haber: {n_desc}/{len(by_link)}, tam metni olan: {n_full}/{len(by_link)}")

    now = datetime.now(timezone.utc)
    total = 0
    for cat, items in cat_lists.items():
        for idx, it in enumerate(items[:5]):  # vitrin haberi resimli olsun
            if it["img"]:
                items.insert(0, items.pop(idx))
                break
        total += len(items)
        write_json(cat, items, now)
        print(f"[{cat}] {len(items)} haber, {sum(1 for i in items if i['img'])} resimli")
    write_json("tumu", all_lists, now)
    print(f"[tumu] {len(all_lists)} haber, {sum(1 for i in all_lists if i['img'])} resimli")
    if total == 0:
        print("Hicbir kategoriden haber alinamadi.")
        sys.exit(1)
    encrypt_all()


if __name__ == "__main__":
    main()
