#!/usr/bin/env python3
"""GitHub Actions icin: Google News RSS'ten haberleri cekip news/ klasorune yazar."""
import os
import urllib.parse
import urllib.request

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

# Once son 2 saat; haber azsa 6 saate, o da azsa 24 saate genisler.
WINDOWS = ["2h", "6h", "24h"]
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def fetch_xml(query, window):
    q = urllib.parse.quote(f"{query} when:{window}")
    url = f"https://news.google.com/rss/search?q={q}&hl=tr&gl=TR&ceid=TR:tr"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read()


def main():
    os.makedirs("news", exist_ok=True)
    ok = 0
    for cat, query in QUERIES.items():
        best = None
        for window in WINDOWS:
            try:
                xml = fetch_xml(query, window)
            except Exception as e:
                print(f"{cat} {window}: hata {e}")
                continue
            best = xml
            if xml.count(b"<item>") >= 3:
                break
        if best:
            with open(os.path.join("news", f"{cat}.xml"), "wb") as f:
                f.write(best)
            ok += 1
            print(f"{cat}: {best.count(b'<item>')} haber")
        else:
            print(f"{cat}: alinamadi")
    print(f"{ok}/{len(QUERIES)} kategori guncellendi")


if __name__ == "__main__":
    main()
