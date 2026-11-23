/* HaberAkış APK: haberleri telefon kendisi çeker (GitHub gerekmez).
   Yalnızca Android uygulamasında çalışır; tarayıcıda hiçbir şey yapmaz.
   index.html'den önce yüklenir ve window.NativeNews.get(url) sağlar. */
(function () {
  var C = window.Capacitor;
  if (!(C && C.isNativePlatform && C.isNativePlatform())) return;

  var QUERIES = {
    gundem: "türkiye gündem son dakika", ekonomi: "türkiye ekonomi borsa dolar",
    spor: "türkiye spor futbol", teknoloji: "teknoloji yapay zeka", saglik: "sağlık türkiye",
    dunya: "dünya haberleri son dakika", kultur: "kültür sanat sinema", bilim: "bilim uzay araştırma"
  };
  var CATS = Object.keys(QUERIES);
  var UA = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36";
  var POOL_TTL = 10 * 60 * 1000, MAX_ITEMS = 30, MIN_ITEMS = 6, PER_CAT_ALL = 14, CONC = 4;

  // ---------- yardımcılar ----------
  function jget(k) { try { return JSON.parse(localStorage.getItem(k) || "null"); } catch (e) { return null; } }
  function jset(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} }
  function withTimeout(p, ms) {
    return new Promise(function (res, rej) {
      var t = setTimeout(function () { rej(new Error("zaman aşımı")); }, ms);
      p.then(function (v) { clearTimeout(t); res(v); }, function (e) { clearTimeout(t); rej(e); });
    });
  }
  function http(url, opt) {
    opt = opt || {};
    var headers = Object.assign({ "User-Agent": UA, "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.5" }, opt.headers || {});
    var p = fetch(url, { method: opt.method || "GET", headers: headers, body: opt.body })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.text(); });
    return withTimeout(p, opt.timeout || 20000);
  }
  function unesc(s) { var t = document.createElement("textarea"); t.innerHTML = s; return t.value; }
  function cleanText(raw) { return unesc(String(raw || "").replace(/<[^>]*>/g, " ")).replace(/\s+/g, " ").trim(); }
  function hid(s) { var h = 0; for (var i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0; return "h" + (h >>> 0).toString(36); }
  function titleKey(t) { return t.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, "").slice(0, 60); }

  // ---------- Google News RSS ----------
  function tagText(b, n) {
    var m = b.match(new RegExp("<" + n + "(?:\\s[^>]*)?>([\\s\\S]*?)</" + n + ">", "i"));
    return m ? m[1].replace(/^\s*<!\[CDATA\[([\s\S]*?)\]\]>\s*$/, "$1") : "";
  }
  function parseRSS(xml) {
    var out = [], re = /<item\b[^>]*>([\s\S]*?)<\/item>/gi, m;
    while ((m = re.exec(xml))) {
      var b = m[1], title = cleanText(tagText(b, "title")), link = unesc(tagText(b, "link")).trim();
      var t = Date.parse(tagText(b, "pubDate").trim());
      if (!title || !link || isNaN(t)) continue;
      var source = cleanText(tagText(b, "source")) || "Google Haberler";
      if (title.endsWith(" - " + source)) title = title.slice(0, -(source.length + 3));
      out.push({ title: title, link: link, source: source, dt: Math.min(t, Date.now()), cat: "" });
    }
    return out;
  }
  function pickRecent(items, steps, min, max) {
    var now = Date.now(), chosen = [];
    for (var i = 0; i < steps.length; i++) {
      chosen = items.filter(function (x) { return now - x.dt <= steps[i] * 3600000; });
      if (chosen.length >= min) break;
    }
    var queues = {}, order = [];
    chosen.slice().sort(function (a, b) { return b.dt - a.dt; }).forEach(function (x) {
      if (!queues[x.source]) { queues[x.source] = []; order.push(x.source); }
      queues[x.source].push(x);
    });
    var mixed = [];
    while (order.length && mixed.length < max) {
      for (var j = 0; j < order.length && mixed.length < max; j++) mixed.push(queues[order[j]].shift());
      order = order.filter(function (n) { return queues[n].length; });
    }
    return mixed.sort(function (a, b) { return b.dt - a.dt; });
  }

  var poolMem = {};
  async function getPool(cat) {
    var c = poolMem[cat];
    if (c && Date.now() - c.at < POOL_TTL) return c.items;
    var items = [], ws = ["24h", "72h"];
    for (var i = 0; i < ws.length; i++) {
      try {
        var url = "https://news.google.com/rss/search?q=" + encodeURIComponent(QUERIES[cat] + " when:" + ws[i]) + "&hl=tr&gl=TR&ceid=TR:tr";
        items = parseRSS(await http(url));
      } catch (e) { continue; }
      if (items.length >= MIN_ITEMS) break;
    }
    var seen = {}, uniq = [];
    items.sort(function (a, b) { return b.dt - a.dt; }).forEach(function (x) {
      var k = titleKey(x.title); if (seen[k]) return; seen[k] = 1; x.cat = cat; uniq.push(x);
    });
    if (!uniq.length) { // ağ yok: son kaydedileni göster
      var saved = jget("haber_pool_" + cat);
      return saved ? saved.items : [];
    }
    poolMem[cat] = { at: Date.now(), items: uniq };
    jset("haber_pool_" + cat, { at: Date.now(), items: uniq });
    return uniq;
  }

  // ---------- haber sayfasından resim / özet / metin ----------
  function b64decode(s) { return atob(s.replace(/-/g, "+").replace(/_/g, "/")); }
  async function decodeGoogle(link) {
    var u = new URL(link);
    if (u.hostname !== "news.google.com") return link;
    var segs = u.pathname.split("/").filter(Boolean);
    if (segs.length < 2 || ["articles", "read"].indexOf(segs[segs.length - 2]) < 0) throw new Error("biçim");
    var id = segs[segs.length - 1];
    try { var m = b64decode(id).match(/https?:\/\/[\x21-\x7e]+/); if (m) return m[0]; } catch (e) {}
    var sg, ts, urls = [
      "https://news.google.com/rss/articles/" + id + "?hl=tr&gl=TR&ceid=TR:tr",
      "https://news.google.com/articles/" + id + "?hl=en-US&gl=US&ceid=US:en"
    ];
    for (var i = 0; i < urls.length; i++) {
      try {
        var page = await http(urls[i], { headers: { Cookie: "CONSENT=YES+cb; SOCS=CAI", "Accept-Language": "en-US,en;q=0.9" }, timeout: 15000 });
        sg = (page.match(/data-n-a-sg=["']([^"']+)/) || [])[1];
        ts = (page.match(/data-n-a-ts=["']([^"']+)/) || [])[1];
        if (sg && ts) break;
      } catch (e) {}
    }
    if (!sg || !ts) throw new Error("imza");
    var inner = '["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],"X","X",1,[1,1,1],1,1,null,0,0,null,0],"' + id + '",' + ts + ',"' + sg + '"]';
    var body = "f.req=" + encodeURIComponent(JSON.stringify([[["Fbv4je", inner]]]));
    var text = await http("https://news.google.com/_/DotsSplashUi/data/batchexecute", {
      method: "POST", body: body, timeout: 15000,
      headers: { "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8", Cookie: "CONSENT=YES+cb; SOCS=CAI" }
    });
    var outer = JSON.parse(text.split("\n\n")[1]);
    return JSON.parse(outer[0][2])[1];
  }

  function metaContent(page, names) {
    for (var i = 0; i < names.length; i++) {
      var n = names[i];
      var pats = [
        new RegExp("<meta[^>]+(?:property|name)=[\"']" + n + "[\"'][^>]*?content=[\"']([^\"']*)", "i"),
        new RegExp("<meta[^>]+content=[\"']([^\"']*)[\"'][^>]*?(?:property|name)=[\"']" + n + "[\"']", "i")
      ];
      for (var j = 0; j < pats.length; j++) {
        var m = page.match(pats[j]);
        if (m && m[1].trim()) return unesc(m[1]).trim();
      }
    }
    return "";
  }
  function shorten(text, limit) {
    limit = limit || 480; text = text.replace(/\s+/g, " ").trim();
    if (text.length <= limit) return text;
    var cut = text.slice(0, limit), end = Math.max(cut.lastIndexOf(". "), cut.lastIndexOf("! "), cut.lastIndexOf("? "));
    return end > limit * 0.5 ? cut.slice(0, end + 1) : cut.slice(0, cut.lastIndexOf(" ")) + "…";
  }
  var BOILER = /abone ol|tüm hakları|çerez|cookie|©|bültene|haberi paylaş|yorum yap|kvkk|gizlilik|whatsapp|telegram|facebook|twitter|instagram|reklam|sosyal medya hesab|google news|devamını oku|ilgili haber|tıklayın|uygulamamızı/i;
  function articleBody(page) {
    page = page.replace(/<(script|style|noscript|iframe|nav|header|footer|aside|form|figure|button)\b[^>]*>[\s\S]*?<\/\1>/gi, " ");
    var arts = page.match(/<article\b[^>]*>[\s\S]*?<\/article>/gi) || [], scope = page;
    if (arts.length) scope = arts.reduce(function (a, b) { return b.length > a.length ? b : a; });
    var paras = [], seen = {}, total = 0, re = /<p\b[^>]*>([\s\S]*?)<\/p>/gi, m;
    while ((m = re.exec(scope))) {
      var p = cleanText(m[1]);
      if (p.length < 40 || seen[p]) continue;
      if (p.length < 220 && BOILER.test(p)) continue;
      seen[p] = 1; paras.push(p); total += p.length;
      if (total > 16000 || paras.length >= 60) break;
    }
    return paras.length >= 2 && total >= 300 ? paras : [];
  }
  function pageInfo(page, pageUrl) {
    var img = metaContent(page, ["og:image:secure_url", "og:image", "twitter:image", "twitter:image:src"]);
    if (img) {
      try { img = new URL(img, pageUrl).href; } catch (e) { img = ""; }
      if (img.indexOf("http://") === 0) img = "https://" + img.slice(7);
      if (img.indexOf("https://") !== 0) img = "";
    }
    var desc = cleanText(metaContent(page, ["og:description", "twitter:description", "description"]));
    var body = articleBody(page);
    if (desc.length < 120 && body.length) desc = (desc + " " + body.slice(0, 3).filter(function (p) { return p !== desc; }).join(" ")).trim();
    return { img: img, desc: shorten(desc), body: body };
  }

  // ---------- meta önbelleği + arka plan kuyruğu ----------
  var meta = jget("haber_meta") || {}, idMap = {}, bodyMem = {}, bodyKeys = [];
  var saveTimer = null;
  function saveMeta() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(function () {
      var keys = Object.keys(meta);
      if (keys.length > 600) {
        keys.sort(function (a, b) { return (meta[a].t || 0) - (meta[b].t || 0); });
        keys.slice(0, keys.length - 600).forEach(function (k) { delete meta[k]; });
      }
      jset("haber_meta", meta);
    }, 1000);
  }
  function rememberBody(link, paras) {
    if (!paras.length) return;
    bodyMem[link] = paras; bodyKeys.push(link);
    if (bodyKeys.length > 60) delete bodyMem[bodyKeys.shift()];
  }
  async function resolveMeta(link) {
    try {
      var real = (meta[link] && meta[link].url) || await decodeGoogle(link);
      var info = pageInfo(await http(real, { timeout: 15000 }), real);
      meta[link] = { img: info.img, desc: info.desc, url: real, t: Date.now() };
      rememberBody(link, info.body);
      saveMeta();
      if (window.patchItemMeta) window.patchItemMeta(link, meta[link]);
    } catch (e) {
      meta[link] = { img: "", desc: "", url: "", t: Date.now() };
      saveMeta();
    }
  }
  var queue = [], queued = {}, running = 0;
  function pump() {
    while (running < CONC && queue.length) {
      var l = queue.shift(); running++;
      resolveMeta(l).then(function () {}, function () {}).then((function (ll) {
        return function () { running--; delete queued[ll]; pump(); };
      })(l));
    }
  }
  function need(link) { var m = meta[link]; return !m || (!m.img && Date.now() - (m.t || 0) > 3 * 3600000); }
  function enqueue(items) {
    items.forEach(function (it) { if (need(it.link) && !queued[it.link]) { queued[it.link] = 1; queue.push(it.link); } });
    pump();
  }

  // ---------- JSON üretimi (sunucudaki news/*.json ile aynı biçim) ----------
  function view(it) {
    var m = meta[it.link] || {}, id = hid(it.link); idMap[id] = it.link;
    return { title: it.title, link: it.link, url: m.url || "", source: it.source, time: new Date(it.dt).toISOString(),
             desc: m.desc || "", img: m.img || "", cat: it.cat, full: true };
  }
  async function buildCategory(cat) {
    var items = pickRecent(await getPool(cat), [24, 72], MIN_ITEMS, MAX_ITEMS);
    enqueue(items);
    return { updated: new Date().toISOString(), items: items.map(view) };
  }
  async function buildAll() {
    var pools = await Promise.all(CATS.map(getPool)), seen = {}, all = [];
    pools.forEach(function (pool) {
      var part = 0;
      pickRecent(pool, [24], 1, PER_CAT_ALL * 2).forEach(function (it) {
        var k = titleKey(it.title);
        if (seen[k] || part >= PER_CAT_ALL) return;
        seen[k] = 1; part++; all.push(it);
      });
    });
    all.sort(function (a, b) { return b.dt - a.dt; });
    enqueue(all);
    return { updated: new Date().toISOString(), items: all.map(view) };
  }
  async function getBody(id) {
    var link = idMap[id];
    if (!link) throw new Error("bilinmeyen haber");
    if (!bodyMem[link]) {
      var real = (meta[link] && meta[link].url) || await decodeGoogle(link);
      var info = pageInfo(await http(real, { timeout: 15000 }), real);
      meta[link] = { img: info.img || (meta[link] || {}).img || "", desc: info.desc, url: real, t: Date.now() };
      saveMeta(); rememberBody(link, info.body);
      if (window.patchItemMeta) window.patchItemMeta(link, meta[link]);
    }
    if (!bodyMem[link]) throw new Error("metin yok");
    return { p: bodyMem[link] };
  }

  window.NativeNews = {
    get: async function (url) {
      var path = url.split("?")[0], m;
      if ((m = path.match(/^news\/a\/(h[0-9a-z]+)\.json$/))) return getBody(m[1]);
      if ((m = path.match(/^news\/([a-z]+)\.json$/))) return m[1] === "tumu" ? buildAll() : buildCategory(m[1]);
      throw new Error("bilinmeyen adres");
    },
    refresh: function () { poolMem = {}; },
    // Haber resmini telefon indirir ve data: adresi olarak verir (yüz süzgeci için çapraz-köken sorunu olmaz)
    image: async function (url) {
      var H = C.Plugins && C.Plugins.CapacitorHttp;
      if (!H) throw new Error("http yok");
      var r = await withTimeout(H.request({ url: url, method: "GET", responseType: "blob",
        headers: { "User-Agent": UA, "Accept": "image/*,*/*;q=0.5" } }), 20000);
      if (!r || r.status >= 400 || !r.data) throw new Error("resim yok");
      var h = r.headers || {}, ct = String(h["Content-Type"] || h["content-type"] || "image/jpeg").split(";")[0].trim();
      if (ct.indexOf("image/") !== 0) ct = "image/jpeg";
      return "data:" + ct + ";base64," + r.data;
    }
  };

  // ---------- Android geri tuşu ----------
  try {
    var App = C.registerPlugin("App");
    App.addListener("backButton", function () { if (!(window.handleBack && window.handleBack())) App.exitApp(); });
  } catch (e) {}
})();
