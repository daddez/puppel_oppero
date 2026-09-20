"""Funzioni condivise tra i motori Safari (download semplice, agente di solo testo, e l'agente
visivo tenuto come traccia ma non più usato). Nessuna immagine qui dentro: solo Selenium e testo."""
import math
import random
import time

from selenium.webdriver.common.actions.action_builder import ActionBuilder

# Impronta della STRUTTURA della pagina (tag+classi normalizzate+profondità, MAI testo/numeri): usata
# per il vocabolario dei layout, per riconoscere un sito già visto e riusare la ricetta senza AI.
JS_STRUTTURA = r"""
const norm = s => (s||'').toLowerCase().replace(/[0-9]+/g,'#').replace(/[^a-z#_-]/g,'');
const ammessi = new Set(['header','nav','main','footer','aside','section','article','form','ul','ol','table','div','a','button','input','h1','h2','h3']);
const out = new Set(); let n = 0;
for (const e of document.querySelectorAll('body *')) {
  if (++n > 1500) break;
  const t = e.tagName.toLowerCase(); if (!ammessi.has(t)) continue;
  const cls = [...e.classList].map(norm).filter(c => c && c.length < 24).sort().slice(0,3).join('.');
  const id = norm(e.id).slice(0,20);
  if (t === 'div' && !cls && !id) continue;
  let d = 0, p = e; while (p && p !== document.body && d < 12) { p = p.parentElement; d++; }
  out.add(t + (id ? '#'+id : '') + (cls ? '.'+cls : '') + '@' + Math.min(d,6));
}
return [...out].slice(0,400);
"""


def parole_titolo(t):
    import re
    import unicodedata
    s = re.sub(r"[̀-ͯ]", "", unicodedata.normalize("NFD", t.lower()))
    return list(dict.fromkeys(w for w in re.split(r"[^a-z0-9]+", s) if len(w) >= 5))


def copertura(titolo, testo):
    import re
    import unicodedata
    p = parole_titolo(titolo)
    if len(p) < 3:
        return 1.0
    t = re.sub(r"[̀-ͯ]", "", unicodedata.normalize("NFD", testo.lower()))
    return sum(1 for w in p if w in t) / len(p)


def jaccard(a, b):
    a, b = set(a), set(b)
    return len(a & b) / len(a | b) if (a or b) else 0.0


def assicura_pagina_completa(d, tentativi_max=8):
    """Scorre fino in fondo (contenuto a caricamento pigro/scorrimento infinito compreso) e poi torna
    su: serve a far comparire nel DOM ciò che un sito moderno carica solo quando ci si scorre vicino,
    PRIMA di leggerlo come testo (page_source, mappa dei link, impronta di struttura). Si ferma quando
    l'altezza della pagina smette di crescere."""
    altezza_prima = -1
    for _ in range(tentativi_max):
        altezza = d.execute_script("return document.body.scrollHeight")
        if altezza == altezza_prima:
            break
        altezza_prima = altezza
        d.execute_script("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(0.7)
    d.execute_script("window.scrollTo(0, 0)")
    time.sleep(0.3)


def percorso_curvo(p0, p1, passi=14):
    """Punti lungo una curva di Bézier con scostamento laterale e piccola sbavatura: il cursore non teletrasporta."""
    (x0, y0), (x1, y1) = p0, p1
    dx, dy = x1 - x0, y1 - y0
    lung = math.hypot(dx, dy) or 1.0
    nx, ny = -dy / lung, dx / lung
    off = random.uniform(-0.18, 0.18) * lung + random.choice([-1, 1]) * random.uniform(8, 30)
    cx, cy = (x0 + x1) / 2 + nx * off, (y0 + y1) / 2 + ny * off
    pts = []
    for i in range(1, passi + 1):
        t = i / passi
        t = t * t * (3 - 2 * t)  # parte piano, accelera, rallenta
        x = (1 - t) ** 2 * x0 + 2 * (1 - t) * t * cx + t ** 2 * x1
        y = (1 - t) ** 2 * y0 + 2 * (1 - t) * t * cy + t ** 2 * y1
        pts.append((x + random.uniform(-1, 1) * (1 - t), y + random.uniform(-1, 1) * (1 - t)))
    return pts


class Cursore:
    def __init__(self, d):
        self.d = d
        self.pos = (random.uniform(200, 500), random.uniform(150, 350))

    def clicca_in(self, x, y, w=20, h=20):
        # punto casuale nella parte centrale dell'elemento (non sempre il centro esatto)
        x += random.uniform(-0.2, 0.2) * w
        y += random.uniform(-0.2, 0.2) * h
        ab = ActionBuilder(self.d)
        for px, py in percorso_curvo(self.pos, (x, y)):
            ab.pointer_action.move_to_location(int(px), int(py))
            ab.pointer_action.pause(random.uniform(0.008, 0.028))
        ab.pointer_action.pause(random.uniform(0.09, 0.2))
        ab.pointer_action.click()
        ab.perform()
        self.pos = (x, y)
