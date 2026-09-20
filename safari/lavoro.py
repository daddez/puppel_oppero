#!/usr/bin/env python3
"""
Esecutore MUTO su Safari vero (runner macOS di GitHub). Non decide nulla e non contiene né prompt né
modelli né chiavi di AI o di R2: tutta la logica sta sul server MiND.

Cosa fa, in ordine:
  1. legge dal secret SESSIONE l'indirizzo unico e il token a scadenza del suo lotto;
  2. chiede al server il lavoro successivo, apre la pagina, la scorre fino in fondo e ne legge il TESTO
     (mai il codice HTML, gli attributi o le classi);
  3. manda il testo al server e ESEGUE alla lettera l'ordine ricevuto (click, scarica, indietro, fine);
  4. raccoglie pagina e allegati (con i cookie di Safari) e li carica con gli URL pre-firmati che il
     server genera apposta per quel lavoro;
  5. comunica il risultato e passa al lavoro dopo, finché il lotto è finito.
"""
import os, re, sys, time, urllib.parse

import requests
from selenium import webdriver

sys.path.insert(0, os.path.dirname(__file__))
from comune import JS_STRUTTURA, Cursore, assicura_pagina_completa  # noqa: E402

MAX_PASSI = 10
TEMPO_LAVORO_S = 240
TEMPO_TOTALE_S = 45 * 60
MAX_ALLEGATI = 8
MAX_BYTE = 30 * 1024 * 1024
ESTENSIONI = r"\.(pdf|docx?|xlsx?|odt|ods|pptx?|csv|rtf|p7m)(\?|$)"
PAROLE = r"allegat|avviso|bando|decreto|testo integrale|modulistica"
DOMINI_ANTIROBOT = ("perfdrive.com", "radware.com")
OID_FIRMA = bytes.fromhex("06092a864886f70d010702")

JS_MAPPA = r"""
const cand = Array.from(document.querySelectorAll('a[href], button, input[type=submit], input[type=search], [role=button], summary'));
const out = []; let n = 0;
for (const e of cand) {
  const cs = getComputedStyle(e);
  if (cs.visibility === 'hidden' || cs.display === 'none' || Number(cs.opacity) === 0) continue;
  const testo = (e.innerText || e.value || e.getAttribute('aria-label') || e.title || '').replace(/\s+/g,' ').trim().slice(0,140);
  if (!testo) continue;
  if (++n > 250) break;
  out.push({testo, href: e.href || ''});
}
return out;
"""

JS_TROVA = r"""
const cercato = (arguments[0] || '').toLowerCase().trim();
const cand = Array.from(document.querySelectorAll('a[href], button, input[type=submit], input[type=search], [role=button], summary'));
let migliore = null, punteggio = -1;
for (const e of cand) {
  const cs = getComputedStyle(e);
  if (cs.visibility === 'hidden' || cs.display === 'none' || Number(cs.opacity) === 0) continue;
  const t = (e.innerText || e.value || e.getAttribute('aria-label') || e.title || '').replace(/\s+/g,' ').trim().toLowerCase();
  if (!t) continue;
  let p = -1;
  if (t === cercato) p = 100;
  else if (t.includes(cercato) || cercato.includes(t)) p = 50 - Math.abs(t.length - cercato.length);
  if (p > punteggio) { punteggio = p; migliore = e; }
}
if (!migliore || punteggio < 0) return null;
migliore.scrollIntoView({block: 'center'});
const r = migliore.getBoundingClientRect();
return {href: migliore.href || '', testo: (migliore.innerText || migliore.value || '').replace(/\s+/g,' ').trim(),
        x: r.left + r.width / 2, y: r.top + r.height / 2, w: r.width, h: r.height};
"""


def dominio_base(url):
    try:
        return ".".join(urllib.parse.urlparse(url).hostname.split(".")[-2:])
    except Exception:  # noqa: BLE001
        return ""


def formato(buf, ext):
    testa = buf[:48]
    if (buf[:1] == b"\x30" and OID_FIRMA in testa) or buf[:11] == b"-----BEGIN " or (buf[:2] == b"MI" and ext == "p7m"):
        return "p7m"
    if buf[:5] == b"%PDF-":
        return "pdf"
    if buf[:4] == b"PK\x03\x04":
        return "zip"
    if buf[:4] == b"\xd0\xcf\x11\xe0":
        return "ole"
    if ext in ("csv", "rtf", "txt"):
        return ext
    return "altro"


def scegli_allegati(link, base):
    punteggi = {}
    for l in link:
        href, testo = l.get("href") or "", (l.get("testo") or "").strip()
        if not href or href.startswith(("#", "mailto:", "javascript:")):
            continue
        url = urllib.parse.urljoin(base, href)
        if dominio_base(url) != dominio_base(base) or url.split("#")[0] == base.split("#")[0]:
            continue
        p = (3 if re.search(ESTENSIONI, href, re.I) else 0) + (2 if re.search(PAROLE, f"{href} {testo}", re.I) else 0)
        if p > 0:
            punteggi[url] = max(p, punteggi.get(url, 0))
    return [u for u, _ in sorted(punteggi.items(), key=lambda x: -x[1])][:MAX_ALLEGATI]


# ── Comunicazione col server ───────────────────────────────────────────────────────────────────

class Server:
    def __init__(self, base, token):
        self.base, self.token = base.rstrip("/"), token

    def chiama(self, azione, corpo=None):
        for tentativo in range(3):
            try:
                r = requests.post(f"{self.base}/{azione}", json=corpo or {}, headers={"Authorization": f"Bearer {self.token}"}, timeout=90)
                if r.status_code == 401:
                    sys.exit("sessione scaduta o revocata")
                if r.ok:
                    return r.json()
                print(f"  server {azione}: HTTP {r.status_code}", flush=True)
            except requests.RequestException as e:
                print(f"  server {azione}: {type(e).__name__}", flush=True)
            time.sleep(2 * (tentativo + 1))
        return None


# ── Un lavoro ──────────────────────────────────────────────────────────────────────────────────

def esegui_lavoro(d, srv, lavoro):
    t0 = time.time()
    lid, url0 = lavoro["id"], lavoro["url"]
    host = urllib.parse.urlparse(url0).hostname or "sito"
    consentiti = {dominio_base(url0), *DOMINI_ANTIROBOT}
    da_scaricare, passi_eseguiti, storia = [], [], []
    trovato, motivo, ricetta_id = False, "", None

    def log(x):
        print(f"  {x}"[:200], flush=True)

    def accoda(u):
        if u and u not in da_scaricare and dominio_base(u) == dominio_base(url0):
            da_scaricare.append(u)
            return True
        return False

    def attendi():
        time.sleep(1.8)
        for _ in range(15):
            try:
                if d.execute_script("return document.readyState") == "complete":
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.4)

    def documento_aperto():
        try:
            tipo = d.execute_script("return document.contentType || ''")
        except Exception:  # noqa: BLE001
            tipo = ""
        if re.search(ESTENSIONI, d.current_url, re.I) or "pdf" in tipo or "msword" in tipo or "officedocument" in tipo:
            accoda(d.current_url)
            d.back()
            attendi()
            return True
        return False

    def leggi():
        assicura_pagina_completa(d)
        testo = d.execute_script("return (document.body ? document.body.innerText : '').replace(/\\s+/g,' ')") or ""
        return testo, d.execute_script(JS_MAPPA)

    def esegui(az, elemento, cursore):
        """Esegue UN ordine del server. Ritorna False se l'elemento non c'è."""
        el = d.execute_script(JS_TROVA, elemento)
        if not el:
            return False
        if az == "scarica" or re.search(ESTENSIONI, el["href"] or "", re.I):
            accoda(el["href"])
        else:
            cursore.clicca_in(el["x"], el["y"], el["w"], el["h"])
            attendi()
            documento_aperto()
            if dominio_base(d.current_url) not in consentiti:
                d.back()
                attendi()
        return True

    d.get(url0)
    attendi()
    cursore = Cursore(d)
    assicura_pagina_completa(d)
    struttura = d.execute_script(JS_STRUTTURA)
    log(f"apro «{d.title[:60]}»")

    ini = srv.chiama("inizio", {"lavoroId": lid, "host": host, "struttura": struttura}) or {}
    ric = ini.get("ricetta")
    if ric:
        ricetta_id = ric["id"]
        log(f"ricetta del server ({len(ric['passi'])} passi)")
        ok = True
        for p in ric["passi"]:
            if not esegui(p.get("azione"), p.get("elemento", ""), cursore):
                ok = False
                break
        trovato = ok  # la pertinenza la verifica il server dopo
        if not ok:
            log("la ricetta non vale più: si riparte con le istruzioni del server")
            da_scaricare.clear()
            d.get(url0)
            attendi()
            ricetta_id, trovato = None, False

    if not trovato:
        for n in range(1, MAX_PASSI + 1):
            if time.time() - t0 > TEMPO_LAVORO_S:
                motivo = "tempo massimo"
                break
            testo, elementi = leggi()
            r = srv.chiama("passo", {"lavoroId": lid, "storia": storia, "titoloPagina": d.title, "urlPagina": d.current_url, "testoPagina": testo, "elementi": elementi})
            dec = (r or {}).get("decisione")
            if not dec:
                motivo = "server non raggiungibile"
                break
            az, el = str(dec.get("azione", "")), str(dec.get("elemento", "")).strip()
            if az == "fine":
                trovato, motivo = dec.get("esito") == "trovato", str(dec.get("motivo", ""))
                log(f"fine: {dec.get('esito')} {motivo}")
                break
            if az == "indietro":
                d.back()
                attendi()
                storia.append(f"passo {n}: indietro")
                continue
            if az not in ("click", "scarica") or not el:
                storia.append(f"passo {n}: risposta non valida")
                continue
            if esegui(az, el, cursore):
                passi_eseguiti.append({"azione": az, "elemento": el[:120]})
                storia.append(f"passo {n}: {az} «{el[:50]}»")
                log(f"{az} «{el[:50]}»")
            else:
                storia.append(f"passo {n}: elemento «{el[:50]}» non trovato")

    file_ok, url_fin = [], d.current_url
    if trovato:
        assicura_pagina_completa(d)
        url_fin = d.current_url
        html = d.page_source or ""
        link = d.execute_script("return Array.from(document.querySelectorAll('a[href]')).slice(0,600).map(a=>({href:a.href,testo:(a.innerText||a.title||'').replace(/\\s+/g,' ').trim().slice(0,120)}))")
        for u in scegli_allegati(link, url_fin):
            accoda(u)
        ua = d.execute_script("return navigator.userAgent")
        sess = requests.Session()
        for c in d.get_cookies():
            sess.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/"))
        sess.headers.update({"User-Agent": ua, "Referer": url_fin, "Accept": "*/*"})
        blobs = [("html", html.encode("utf-8"), url_fin)]
        for u in da_scaricare[:MAX_ALLEGATI]:
            try:
                r = sess.get(u, timeout=60, stream=True)
                if r.status_code != 200:
                    continue
                buf = b""
                for pezzo in r.iter_content(65536):
                    buf += pezzo
                    if len(buf) > MAX_BYTE:
                        break
                m = re.search(r"\.([A-Za-z0-9]{2,5})(\?|$)", urllib.parse.urlparse(u).path + "?")
                ext = m.group(1).lower() if m else ""
                if buf and len(buf) <= MAX_BYTE and formato(buf, ext) != "altro":
                    blobs.append((ext or "bin", buf, u))
                    log(f"allegato {len(buf) // 1024} KB: {u[-60:]}")
            except Exception as e:  # noqa: BLE001
                log(f"allegato non scaricato ({type(e).__name__}): {u[-50:]}")
        # URL pre-firmati unici, generati dal server per questo solo lavoro
        c = srv.chiama("carica", {"lavoroId": lid, "tipi": [b[0] for b in blobs]}) or {}
        for spec, (ext, buf, u) in zip(c.get("file", []), blobs):
            pr = requests.put(spec["uploadUrl"], data=buf, headers={"Content-Type": "application/octet-stream"}, timeout=120)
            if pr.status_code in (200, 201):
                file_ok.append({"nome": spec["nome"], "url": u})
        if not any(f["nome"] == "pagina.html" for f in file_ok):
            trovato, motivo = False, "caricamento non riuscito"

    srv.chiama("fine", {"lavoroId": lid, "host": host, "struttura": struttura, "ricettaIdUsata": ricetta_id, "trovato": trovato,
                        "passiEseguiti": passi_eseguiti, "urlFinale": url_fin, "file": file_ok, "motivo": motivo})
    log(f"lavoro concluso: {'trovato' if trovato else 'non trovato'} ({len(file_ok)} file, {round(time.time() - t0)}s)")


def main():
    segreto = os.environ.get("SESSIONE", "")
    if "|" not in segreto:
        sys.exit("secret di sessione mancante")
    base, token = segreto.split("|", 1)
    for parte in (base, token):
        print(f"::add-mask::{parte}")
    srv = Server(base, token)
    d = webdriver.Safari()
    d.set_window_size(1280, 900)
    d.set_page_load_timeout(60)
    inizio, n = time.time(), 0
    try:
        while time.time() - inizio < TEMPO_TOTALE_S:
            r = srv.chiama("prossimo")
            if not r or r.get("fine") or not r.get("lavoro"):
                break
            n += 1
            print(f"Lavoro {n}: {r['lavoro']['bando']['titolo'][:70]}", flush=True)
            try:
                d.delete_all_cookies()
            except Exception:  # noqa: BLE001
                pass
            try:
                esegui_lavoro(d, srv, r["lavoro"])
            except Exception as e:  # noqa: BLE001
                print(f"  errore: {type(e).__name__}: {str(e)[:120]}", flush=True)
                srv.chiama("fine", {"lavoroId": r["lavoro"]["id"], "trovato": False, "motivo": f"errore worker: {type(e).__name__}", "file": []})
    finally:
        d.quit()
    print(f"Lotto finito: {n} lavori")


if __name__ == "__main__":
    main()
