# safari_crawler_mind — esecutore

Esecutore generico di browser (Safari su macOS, GitHub Actions). Non contiene logica di decisione,
prompt, modelli né chiavi: legge il testo di una pagina, lo invia al server che lo lancia e ne esegue
gli ordini alla lettera. Indirizzo e token del lotto arrivano da un secret monouso creato dal server
per ogni lancio e cancellato a fine lavoro.

- `safari/lavoro.py` — l'esecutore
- `safari/comune.py` — scorrimento, impronta della struttura, cursore
- `.github/workflows/safari-lavoro.yml` — unico workflow (input: ID sessione)
