#!/usr/bin/env python3
"""
Esecutore MUTO su Safari vero (runner macOS di GitHub). Non decide nulla e non contiene né prompt né
modelli né chiavi di AI o di R2: la rete di AI sta sul server MiND.

Cosa fa, in ordine:
  1. legge dal secret SESSIONE l'indirizzo unico e il token a scadenza del suo lotto;
  2. chiede al server il bando successivo (uno alla volta, in sequenza);
  3. per quel bando resta in ascolto: riceve un comando alla volta (leggi pagina, scroll, screenshot,
     click, scarica, indietro, apri indirizzo, raccogli, carica), lo esegue e restituisce cosa vede
     (testo e link della pagina, mai codice HTML, attributi o classi; screenshot solo se richiesto);
  4. quando il server chiude la conversazione passa al bando dopo, finché il lotto è finito.
"""
import base64, io, os, re, sys, time, urllib.parse

import requests
import urllib3
from selenium import webdriver

sys.path.insert(0, os.path.dirname(__file__))
from comune import Cursore, assicura_pagina_completa  # noqa: E402

urllib3.disable_warnings()
TEMPO_TOTALE_S = 340 * 60
MAX_ALLEGATI = 8
MAX_BYTE = 30 * 1024 * 1024
ESTENSIONI = r"\.(pdf|docx?|xlsx?|odt|ods|pptx?|csv|rtf|p7m)(\?|$)"
PAROLE = r"allegat|avviso|bando|decreto|testo integrale|modulistica"
DOMINI_ANTIROBOT = ("perfdrive.com", "radware.com")
OID_FIRMA = bytes.fromhex("06092a864886f70d010702")

# Elenco NUMERATO dei link/pulsanti visibili, ciascuno col suo contesto (la riga o il titolo sotto cui sta):
# è ciò che permette all'AI di distinguere dieci link tutti chiamati "Bando".
JS_ELENCO = r"""
const cand = Array.from(document.querySelectorAll('a[href], button, input[type=submit], input[type=search], [role=button], summary')).filter(e => {
  const cs = getComputedStyle(e);
  if (cs.visibility === 'hidden' || cs.display === 'none' || Number(cs.opacity) === 0) return false;
  return (e.innerText || e.value || e.getAttribute('aria-label') || e.title || '').trim().length > 0;
});
const heads = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6'));
const norm = s => (s || '').replace(/\s+/g, ' ').trim();
const contesto = e => {
  const riga = e.closest('li, tr, article, section, .card, [class*=item], [class*=row], [class*=box]');
  const t = riga ? norm(riga.innerText) : '';
  const proprio = norm(e.innerText || e.value || '');
  if (t && t.length <= 320 && t !== proprio) return t.slice(0, 140);
  let h = null;
  for (const x of heads) { if (x.compareDocumentPosition(e) & Node.DOCUMENT_POSITION_FOLLOWING) h = x; else break; }
  return h ? 'sotto «' + norm(h.innerText).slice(0, 100) + '»' : '';
};
if (arguments[0] === 'rect') {
  const e = cand[arguments[1]];
  if (!e) return null;
  e.scrollIntoView({block: 'center'});
  const r = e.getBoundingClientRect();
  return {href: e.href || '', testo: norm(e.innerText || e.value || ''), x: r.left + r.width / 2, y: r.top + r.height / 2, w: r.width, h: r.height};
}
return cand.slice(0, 300).map((e, i) => ({i, testo: norm(e.innerText || e.value || e.getAttribute('aria-label') || e.title).slice(0, 140), href: e.href || '', contesto: contesto(e)}));
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


class Lavoro:
    """Stato del browser per UN bando: pagina corrente, file messi in coda, file pronti da caricare."""

    def __init__(self, d, url_partenza):
        self.d = d
        self.dominio = dominio_base(url_partenza)
        self.consentiti = {self.dominio, *DOMINI_ANTIROBOT}
        self.cursore = Cursore(d)
        self.in_coda = []
        self.pronti = []  # (ext, bytes, url) — pagina in testa

    def attendi(self):
        time.sleep(1.8)
        for _ in range(15):
            try:
                if self.d.execute_script("return document.readyState") == "complete":
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.4)

    def firma(self):
        try:
            return self.d.current_url + "|" + str(self.d.execute_script("return document.body ? document.body.innerText.length : 0"))
        except Exception:  # noqa: BLE001
            return ""

    def osserva(self, completa=True, screenshot=False, nota=None):
        d = self.d
        if completa:
            assicura_pagina_completa(d)
        testo = d.execute_script("return (document.body ? document.body.innerText : '').replace(/\\s+/g,' ')") or ""
        o = {"ok": True, "url": d.current_url, "titolo": d.title, "testo": testo[:30000], "elementi": d.execute_script(JS_ELENCO)}
        if nota:
            o["nota"] = nota
        if screenshot:
            from PIL import Image
            im = Image.open(io.BytesIO(base64.b64decode(d.get_screenshot_as_base64()))).convert("RGB")
            im.thumbnail((1100, 1100))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=65)
            o["screenshot"] = base64.b64encode(buf.getvalue()).decode()
        return o

    def accoda(self, url):
        if url and url not in self.in_coda and dominio_base(url) == self.dominio:
            self.in_coda.append(url)
            return True
        return False

    def documento_aperto(self):
        try:
            tipo = self.d.execute_script("return document.contentType || ''")
        except Exception:  # noqa: BLE001
            tipo = ""
        if re.search(ESTENSIONI, self.d.current_url, re.I) or "pdf" in tipo or "msword" in tipo or "officedocument" in tipo:
            self.accoda(self.d.current_url)
            self.d.back()
            self.attendi()
            return True
        return False

    def elemento(self, indice):
        return self.d.execute_script(JS_ELENCO, "rect", int(indice))

    def esegui(self, azione, p):
        d = self.d
        if azione == "leggi_pagina":
            return self.osserva()
        if azione == "apri_url":
            url = str(p.get("url") or "")
            if dominio_base(url) not in self.consentiti:
                return {"ok": False, "errore": "indirizzo di un altro sito: non consentito"}
            d.get(url)
            self.attendi()
            return self.osserva()
        if azione in ("scroll_giu", "scroll_su"):
            d.execute_script(f"window.scrollBy(0, {'' if azione == 'scroll_giu' else '-'}Math.round(window.innerHeight * 0.9))")
            time.sleep(0.8)
            return self.osserva(completa=False)
        if azione == "screenshot":
            return self.osserva(completa=False, screenshot=True)
        if azione == "indietro":
            d.back()
            self.attendi()
            return self.osserva()
        if azione in ("click", "scarica"):
            el = self.elemento(p.get("indice", -1))
            if not el:
                return {"ok": False, "errore": f"nessun elemento con indice {p.get('indice')}: rileggi la pagina"}
            if azione == "scarica" or re.search(ESTENSIONI, el["href"] or "", re.I):
                self.accoda(el["href"])
                return self.osserva(completa=False, nota=f"file messo in coda per lo scaricamento: «{el['testo'][:60]}»")
            prima = self.firma()
            self.cursore.clicca_in(el["x"], el["y"], el["w"], el["h"])
            self.attendi()
            self.documento_aperto()
            if dominio_base(d.current_url) not in self.consentiti:
                d.back()
                self.attendi()
                return self.osserva(nota="il click portava a un altro sito: sono tornato indietro")
            o = self.osserva()
            if self.firma() == prima:
                o["nota"] = "ATTENZIONE: dopo il click la pagina è identica a prima (stesso indirizzo e stessa lunghezza): probabilmente non è cambiato nulla"
            return o
        if azione == "raccogli":
            return self.raccogli()
        if azione == "carica":
            return self.carica(p.get("slots") or [])
        return {"ok": False, "errore": f"azione sconosciuta: {azione}"}

    def raccogli(self):
        d = self.d
        assicura_pagina_completa(d)
        url_fin = d.current_url
        html = d.page_source or ""
        link = d.execute_script("return Array.from(document.querySelectorAll('a[href]')).slice(0,600).map(a=>({href:a.href,testo:(a.innerText||a.title||'').replace(/\\s+/g,' ').trim().slice(0,120)}))")
        for u in scegli_allegati(link, url_fin):
            self.accoda(u)
        ua = d.execute_script("return navigator.userAgent")
        sess = requests.Session()
        for c in d.get_cookies():
            sess.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/"))
        sess.headers.update({"User-Agent": ua, "Referer": url_fin, "Accept": "*/*"})
        self.pronti = [("html", html.encode("utf-8"), url_fin)]
        for u in self.in_coda[:MAX_ALLEGATI]:
            try:
                try:
                    r = sess.get(u, timeout=60, stream=True)
                except requests.exceptions.SSLError:
                    # catena di certificati incompleta sul sito dell'ente (Safari la tollera): file pubblico dello
                    # stesso dominio, il server ne verifica comunque il contenuto
                    r = sess.get(u, timeout=60, stream=True, verify=False)
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
                    self.pronti.append((ext or "bin", buf, u))
            except Exception:  # noqa: BLE001
                pass
        return {"ok": True, "url": url_fin, "file": [{"ext": e, "url": u, "byte": len(b)} for e, b, u in self.pronti[1:]]}

    def carica(self, slots):
        caricati = []
        for spec, (ext, buf, u) in zip(slots, self.pronti):
            pr = requests.put(spec["uploadUrl"], data=buf, headers={"Content-Type": "application/octet-stream"}, timeout=120)
            if pr.status_code in (200, 201):
                caricati.append({"nome": spec["nome"], "url": u})
        return {"ok": bool(caricati), "caricati": caricati, "errore": None if caricati else "caricamento su R2 non riuscito"}


def conversazione(d, srv, lavoro):
    lid = lavoro["id"]
    lv = Lavoro(d, lavoro["url"])
    vuoti = 0
    while True:
        r = srv.chiama("comando", {"lavoroId": lid})
        if r is None:
            vuoti += 1
            if vuoti >= 3:
                print("  server non raggiungibile: abbandono questo bando", flush=True)
                return
            continue
        vuoti = 0
        if r.get("fine"):
            return
        c = r.get("comando")
        if not c:
            continue
        t = time.time()
        try:
            oss = lv.esegui(c["azione"], c.get("parametri") or {})
        except Exception as e:  # noqa: BLE001
            oss = {"ok": False, "errore": f"{type(e).__name__}: {str(e)[:150]}"}
        print(f"  {c['azione']} → {'ok' if oss.get('ok') else 'errore'} ({time.time() - t:.1f}s)", flush=True)
        srv.chiama("risultato", {"lavoroId": lid, "comandoId": c["id"], **oss})


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
            print(f"Bando {n}: {r['lavoro']['titolo'][:70]}", flush=True)
            try:
                d.delete_all_cookies()
            except Exception:  # noqa: BLE001
                pass
            try:
                conversazione(d, srv, r["lavoro"])
            except Exception as e:  # noqa: BLE001
                print(f"  errore: {type(e).__name__}: {str(e)[:120]}", flush=True)
    finally:
        d.quit()
    print(f"Lotto finito: {n} bandi")


if __name__ == "__main__":
    main()
