#!/usr/bin/env python3
"""Avesis'ten yayın listelerini çeker, yayinlar.json üretir.

Kaynak liste: Kadro sayfası (Google Sheet, yayımlanmış CSV). Her kişi için
https://avesis.bozok.edu.tr/<avesis>/yayinlar sayfası okunur.

Kullanım:  python fetch_yayinlar.py [--kadro URL_veya_dosya] [--base URL] [--out dosya]
Sonuç özeti stdout'a ve (varsa) $GITHUB_STEP_SUMMARY dosyasına yazılır.
"""
import argparse, copy, csv, io, json, os, re, sys, time, unicodedata, urllib.request, urllib.error
from datetime import datetime, timezone
from bs4 import BeautifulSoup

KADRO = ("https://docs.google.com/spreadsheets/d/e/2PACX-1vQjTsFIQQjUArN0s2jeskw9RYyngWC96n249nC46PPA3-oMelVVC1ayc9T9Uol3W04OfAKOBQT504GN/"
         "pub?gid=1089692875&single=true&output=csv")
BASE = "https://avesis.bozok.edu.tr"
UA = "Mozilla/5.0 (compatible; BozokIlahiyatYayinlar/1.0; +https://ovgeo.github.io/yayinlar/)"
GAP = 1.5  # saniye: sunucuyu yormamak için istekler arası bekleme


def fold(s):
    s = (s or "").replace("İ", "i").replace("I", "ı").lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).replace("ı", "i")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def oku(url, tries=3):
    """(durum, metin). durum: 'ok' | '404' | 'hata'"""
    if not re.match(r"https?://", url):
        return "ok", open(url, encoding="utf-8").read()
    son = ""
    for i in range(tries):
        try:
            rq = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "tr,en;q=0.8"})
            with urllib.request.urlopen(rq, timeout=40) as r:
                return "ok", r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                return "404", ""
            son = f"HTTP {e.code}"
        except Exception as e:
            son = type(e).__name__
        time.sleep(3 * (i + 1))
    return "hata", son


# ---------- Avesis sayfası ayrıştırma ----------
GURULTU = re.compile(r"^(link|creative commons license|sustainable development|plumx|openalex|doi|pdf|"
                     r"atlas.*|metrikler|metrics|\d+)$", re.I)
BOLUM_BASLIK = [
    (re.compile(r"makale|article", re.I), "Makale"),
    (re.compile(r"bildiri|paper.*conference|conference|proceeding", re.I), "Bildiri"),
    (re.compile(r"kitap b[öo]l|book chapter|chapter", re.I), "Kitap bölümü"),
    (re.compile(r"kitap|book", re.I), "Kitap"),
]


def tur_basliktan(h):
    for rx, t in BOLUM_BASLIK:
        if rx.search(h):
            return t
    return ""


def tur_cikar(venue, bolum):
    v = venue.lower()
    if re.search(r"bildiri|congress|kongre|sempozyum|symposium|conference|workshop|çalıştay|calistay", v) and not re.search(r"cilt\.|vol\.", v):
        return "Bildiri"
    if re.search(r"editör|editor|editörler", v):
        return "Kitap bölümü"
    if re.search(r"cilt\.|vol\.|sa\.|no\.", v):
        return "Makale"
    return bolum or "Diğer"


INDEKS = re.compile(r"scopus|\bs?sci\b|sci-|ssci|ahci|esci|tr ?dizin|erih|doaj|web of science|\bwos\b|ebsco|\bmla\b|index|emerging", re.I)


def indeksler(venue):
    """Sondaki parantezleri ayıklar: (TRDizin, Scopus) -> ['TRDizin','Scopus']"""
    m = re.findall(r"\(([^()]*)\)\s*$", venue.strip())
    if not m:
        return [], []
    parcalar = [x.strip() for x in re.split(r"[,;]", m[-1]) if x.strip()]
    idx = [x for x in parcalar if INDEKS.search(x)]
    return idx, [x for x in parcalar if x not in idx]


def yil_cikar(venue):
    y = re.findall(r"(?<!\d)(19[5-9]\d|20[0-4]\d)(?!\d)", venue)
    return int(y[-1]) if y else None


BLOK = ["div", "p", "li", "br", "tr", "td", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "dd", "dt"]


def satir_listesi(kap):
    """Blok öğelerde satır atlayarak, satır içi öğeleri (a, span, em...) aynı satırda tutarak metni böler."""
    k = copy.copy(kap)
    for t in k.find_all(BLOK):
        t.insert_before("\n"); t.insert_after("\n")
    metin = k.get_text("")
    return [re.sub(r"\s+", " ", s).strip() for s in metin.split("\n") if s.strip()]


def sayfa_ayristir(html, base):
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    ana = soup.find("main") or soup.body or soup
    cikti, goruldu = [], set()
    # belge sırasıyla başlıkları ve yayın bağlantılarını gez
    bolum = ""
    for el in ana.descendants:
        if getattr(el, "name", None) in ("h1", "h2", "h3", "h4", "h5", "h6"):
            ht = el.get_text(" ", strip=True)
            if not re.match(r"^\d+\s*[.)]", ht) and not el.find("a", href=re.compile(r"/yayin/")):
                tb = tur_basliktan(ht)
                if tb:
                    bolum = tb
            continue
        if getattr(el, "name", None) != "a":
            continue
        href = el.get("href", "")
        m = re.search(r"/yayin/([0-9a-fA-F-]{8,})", href)
        baslik = el.get_text(" ", strip=True)
        if not m or not baslik or m.group(1) in goruldu:
            continue
        # tek yayın bağlantısı içeren en geniş kapsayıcıyı bul
        kap = el
        while kap.parent is not None and kap.parent is not ana:
            ust = kap.parent
            if len({a.get("href", "").split("?")[0] for a in ust.find_all("a", href=re.compile(r"/yayin/"))}) > 1:
                break
            kap = ust
        satirlar = satir_listesi(kap)
        # numara ve başlığı at
        satirlar = [re.sub(r"^\d+\s*[.)]\s*", "", s) for s in satirlar]
        satirlar = [s for s in satirlar if s and s != baslik and not GURULTU.match(s)]
        yazar = satirlar[0] if satirlar else ""
        venue = satirlar[1] if len(satirlar) > 1 else ""
        # yazar satırı yoksa (ilk satır kaynak gibi görünüyorsa) düzelt
        if yazar and re.search(r"(19|20)\d\d", yazar) and not venue:
            venue, yazar = yazar, ""
        doi = ""
        for a in kap.find_all("a", href=True):
            if "doi.org/" in a["href"]:
                doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", a["href"]); break
        url = href if href.startswith("http") else base + href
        goruldu.add(m.group(1))
        idx, notlar = indeksler(venue)
        cikti.append({
            "baslik": baslik, "yazarlar": yazar, "kaynak": venue,
            "yil": yil_cikar(venue), "tur": tur_cikar(venue, bolum),
            "indeks": idx, "not": notlar, "url": url.split("?")[0], "doi": doi,
        })
    return cikti


# ---------- Kadro ----------
def kadro_oku(kaynak):
    d, metin = oku(kaynak)
    if d != "ok":
        sys.exit(f"Kadro okunamadı ({d} {metin}). Çıkılıyor; mevcut yayinlar.json değişmedi.")
    rows = list(csv.DictReader(io.StringIO(metin)))
    kisiler = []
    for r in rows:
        r = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
        if r.get("aktif", "").lower() in ("hayır", "hayir", "h", "0"):
            continue
        if r.get("grup", "") != "Akademik" or r.get("avesis") == "-":
            continue
        slug = r.get("avesis") or r.get("eposta_adi")
        if not r.get("id") or not slug:
            continue
        kisiler.append({"id": r["id"], "ad_soyad": r.get("ad_soyad", ""), "unvan": r.get("unvan", ""),
                        "bolum": r.get("bolum", ""), "slug": slug})
    return kisiler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kadro", default=KADRO)
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--out", default="yayinlar.json")
    ap.add_argument("--gap", type=float, default=GAP)
    a = ap.parse_args()

    eski = {}
    if os.path.exists(a.out):
        try: eski = json.load(open(a.out, encoding="utf-8"))
        except Exception: eski = {}
    eski_by_kisi = {}
    for y in eski.get("yayinlar", []):
        for k in y.get("kisiler", []):
            eski_by_kisi.setdefault(k, []).append(y)

    kisiler = kadro_oku(a.kadro)
    rapor, havuz = [], {}
    for n, k in enumerate(kisiler):
        if n: time.sleep(a.gap)
        d, m = oku(f"{a.base}/{k['slug']}/yayinlar")
        if d == "ok":
            ent = sayfa_ayristir(m, a.base)
            durum = "ok" if ent else "bos"
        else:
            ent, durum = [], d
        if d != "ok" or not ent:
            # çekilemediyse ya da boş döndüyse, eski veriyi koru (bir aksilikte silinmesin)
            if d != "404" and eski_by_kisi.get(k["id"]):
                ent = [{kk: vv for kk, vv in y.items() if kk != "kisiler"} for y in eski_by_kisi[k["id"]]]
                durum += "+eski"
        k["sayi"], k["durum"] = len(ent), durum
        rapor.append(k)
        for e in ent:
            anahtar = fold(e["baslik"]) + "|" + str(e.get("yil") or "")
            if anahtar in havuz:
                if k["id"] not in havuz[anahtar]["kisiler"]:
                    havuz[anahtar]["kisiler"].append(k["id"])
                for f in ("doi", "yazarlar", "kaynak"):
                    if not havuz[anahtar].get(f) and e.get(f): havuz[anahtar][f] = e[f]
            else:
                e = dict(e); e["kisiler"] = [k["id"]]; havuz[anahtar] = e

    yay = sorted(havuz.values(), key=lambda e: (-(e["yil"] or 0), fold(e["baslik"])))
    # güvenlik: veri aniden çok küçülürse üzerine yazma
    onceki = len(eski.get("yayinlar", []))
    if onceki and len(yay) < onceki * 0.6:
        sys.exit(f"UYARI: yayın sayısı {onceki} -> {len(yay)} düştü; dosya değiştirilmedi. Raporu inceleyin.")

    sonuc = {"guncelleme": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "kisiler": [{kk: k[kk] for kk in ("id", "ad_soyad", "unvan", "bolum", "slug", "sayi")} for k in rapor],
             "yayinlar": yay}
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(sonuc, f, ensure_ascii=False, separators=(",", ":"))

    sorunlu = [k for k in rapor if k["durum"] != "ok"]
    satir = [f"# Yayın verisi güncellendi", f"- Kişi: {len(rapor)} · Yayın (tekilleştirilmiş): {len(yay)}", ""]
    if sorunlu:
        satir.append("## Bakılması gerekenler (Kadro sayfasındaki `avesis` sütununu kontrol edin)")
        ad = {"404": "Avesis sayfası bulunamadı (slug yanlış olabilir)", "bos": "Sayfa açıldı ama yayın okunamadı/yok",
              "hata": "Bağlantı hatası"}
        for k in sorunlu:
            durum = k["durum"].split("+")[0]
            satir.append(f"- {k['unvan']} {k['ad_soyad']} (`{k['slug']}`): {ad.get(durum, durum)}" + (" – eski veri korundu" if "+eski" in k["durum"] else ""))
    else:
        satir.append("Tüm profiller sorunsuz okundu.")
    satir.append("\n## Kişi başına yayın sayısı")
    satir += [f"- {k['ad_soyad']}: {k['sayi']}" for k in rapor]
    out = "\n".join(satir)
    print(out)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8").write(out + "\n")


if __name__ == "__main__":
    main()
