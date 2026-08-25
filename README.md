<!-- mcp-name: io.github.Lucav21/heu-mcp -->

# HEU Legal MCP Server

[![PyPI](https://img.shields.io/pypi/v/heu-mcp.svg)](https://pypi.org/project/heu-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/heu-mcp.svg)](https://pypi.org/project/heu-mcp/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![MCP Registry](https://img.shields.io/badge/MCP%20Registry-io.github.Lucav21%2Fheu--mcp-blue)](https://registry.modelcontextprotocol.io/v0/servers?search=Lucav21/heu-mcp)

MCP server (Model Context Protocol) che collega l'[API HEU Legal](https://heulegal.com) a Claude e a qualsiasi client MCP. Gestisce l'intero ciclo di vita dei documenti con firma elettronica (valida in 180+ paesi) **direttamente in conversazione**: dalla creazione all'invio in firma, dal sollecito al download del fascicolo legale completo.

---

## Cosa puoi fare

| Voglio... | Il server lo fa con... |
|---|---|
| 📄 Vedere i miei documenti e templates | `list_heu_documents`, `list_pdf_documents` |
| ✍️ Mandare un contratto in firma da un template | `create_heu_document`, `create_pdf_document` |
| 🚀 Mandare in firma un PDF che ho sul computer, senza passare dalla piattaforma | `create_pdf_document_from_upload` |
| 🤖 Far mappare i campi firma **all'AI** (analizza il PDF, posiziona i campi, invia) | `locate_pdf_text` + `create_pdf_document_from_upload` |
| 🔔 Sollecitare chi non ha ancora firmato | `prompt_heu_document_signature`, `prompt_pdf_document_signature` |
| 👀 Far *leggere* un contratto all'AI (riassunti, clausole, confronti) senza scaricarlo | `read_heu_document`, `read_pdf_document` |
| 🪪 Estrarre i dati delle parti (P.IVA, codice fiscale, SDI, PEC, indirizzi) | `extract_heu_document_parties`, `extract_pdf_document_parties` |
| 💾 Scaricare il PDF firmato | `download_heu_document_pdf`, `download_pdf_document` |
| ⚖️ Scaricare il fascicolo legale completo (documento + audit trail + artefatti FES) | `download_pdf_bundle`, `download_pdf_audit_trail` |
| 🧩 Creare/modificare templates PDF riutilizzabili via API | `create_pdf_template`, `update_pdf_template`, `preview_pdf_template`, `delete_pdf_template` |
| ❌ Annullare una richiesta di firma inviata per errore | `cancel_pdf_document` |
| 🩺 Controllare che l'API sia raggiungibile | `get_heu_health` |

Due famiglie di oggetti:
- **Documenti nativi HEU** — creati con l'editor in-app della piattaforma (ID a forma di UUID, es. `5135e7b2-196b-...`).
- **PDF caricati** — file PDF con firmatari e campi firma posizionati sopra (ID numerici, es. `68`).

---

## Requisiti

- Python ≥ 3.10
- API key HEU Legal — nella UI: **Profile → API Keys → Generate API Key** (richiede subscription **Enterprise**; massimo 2 chiavi attive)
- Per i flussi da template: almeno un template creato sulla piattaforma (oppure crealo via API con `create_pdf_template`)

## Installazione

**Da PyPI:**

```bash
pip install heu-mcp
```

**Da sorgenti:**

```bash
git clone https://github.com/heulegal/heu-mcp.git
cd heu-mcp
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Configurazione

### Claude Desktop

Modifica `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) o `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "heu": {
      "command": "heu-mcp",
      "args": [],
      "env": {
        "HEU_API_KEY": "la_tua_api_key_qui"
      }
    }
  }
}
```

Se installato da sorgenti con venv:

```json
{
  "mcpServers": {
    "heu": {
      "command": "/path/assoluto/heu-mcp/venv/bin/python",
      "args": ["/path/assoluto/heu-mcp/server.py"],
      "env": {
        "HEU_API_KEY": "la_tua_api_key_qui"
      }
    }
  }
}
```

Riavvia Claude Desktop dopo la modifica.

### Claude Code (CLI)

```bash
claude mcp add heu heu-mcp -e HEU_API_KEY=la_tua_api_key_qui
```

---

## Server remoto (multi-utente)

Oltre alla modalità locale (stdio) descritta sopra, il progetto include un **server remoto** che espone gli stessi 28 tool via HTTPS, così gli utenti si collegano senza installare nulla: inseriscono la propria API key HEU una volta, in un flusso OAuth.

**Caratteristiche:**
- Transport **Streamable HTTP**, autenticazione **OAuth 2.1** con PKCE S256, Dynamic Client Registration e Client ID Metadata Document
- **Multi-utente**: ogni connessione usa la API key del proprio utente, conservata cifrata (Fernet) e mai in chiaro nel database
- Refresh token con **rotazione** e revoca automatica della sessione in caso di riuso sospetto
- I/O di rete asincrono e parsing PDF su threadpool: una richiesta lenta non blocca gli altri utenti

### Avvio locale

```bash
pip install -e ".[remote]"
export HEU_MCP_SECRET_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
export PUBLIC_URL="http://localhost:8080"
export HEU_MCP_DB="./heu-mcp.db"
heu-mcp-remote
```

### Deploy su Fly.io

```bash
fly launch --no-deploy
fly volumes create heu_data --size 1
fly secrets set HEU_MCP_SECRET_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
fly secrets set PUBLIC_URL="https://<nome-app>.fly.dev"
fly deploy
```

### Variabili d'ambiente del server remoto

| Variabile | Descrizione | Default |
|---|---|---|
| `HEU_MCP_SECRET_KEY` | Chiave Fernet per cifrare le API key degli utenti (**richiesta**) | — |
| `PUBLIC_URL` | URL pubblico del servizio, senza slash finale (**richiesta in produzione**) | `http://localhost:8080` |
| `MCP_PATH` | Path dell'endpoint MCP | `/mcp` |
| `HEU_MCP_DB` | Percorso del database SQLite | `/data/heu-mcp.db` |
| `ACCESS_TOKEN_TTL` / `REFRESH_TOKEN_TTL` | Durata token in secondi | 1 ora / 60 giorni |

### Endpoint esposti

| Endpoint | Scopo |
|---|---|
| `/mcp` | Endpoint MCP (richiede bearer token) |
| `/.well-known/oauth-protected-resource` | Metadati risorsa protetta (RFC 9728) |
| `/.well-known/oauth-authorization-server` | Metadati authorization server (RFC 8414) |
| `/register` · `/authorize` · `/token` | Flusso OAuth 2.1 |
| `/healthz` | Liveness probe |

### Variabili d'ambiente

| Variabile | Descrizione | Default |
|-----------|-------------|---------|
| `HEU_API_KEY` | API key HEU Legal (**richiesta**) | — |
| `HEU_BASE_URL` | URL base dell'API | `https://api.heulegal.com/v1` |
| `HEU_DOWNLOAD_DIR` | Cartella dove salvare i file scaricati | `/tmp` |

---

## Riferimento completo dei tool (28)

### 🩺 Health

| Tool | Parametri | Cosa ritorna |
|---|---|---|
| `get_heu_health` | — | `{ message: "ok", status: 200 }` se l'API è operativa |

### 📄 Documenti nativi HEU

| Tool | Parametri | Cosa fa |
|---|---|---|
| `list_heu_documents` | `type` (document/template), `sort` (asc/desc), `created_from` + `created_to` (ISO 8601), `have_editors_signed` — tutti opzionali | Lista documenti/template con stato, membri, firme. ⚠️ Le due date vanno passate **sempre insieme**, altrimenti l'API può restituire risultati incompleti |
| `get_heu_document` | `document_id` | Dettaglio completo: nome, stato (`to_sign`/`in_progress`/`in_review`/`completed`/`signed`), owner, editors, members con `has_signed` e `signed_at`, tags |
| `list_heu_document_placeholders` | `document_id` | Elenco delle chiavi placeholder sostituibili nel testo del template |
| `create_heu_document` ✋ | `source_document_id`, `email_subject`, `email_text`, `email_to` (lista), `document_name`, `document_type`, `placeholders` (mappa chiave→valore) | Crea un documento da un template, sostituisce i placeholder e lo condivide via email ai destinatari |
| `prompt_heu_document_signature` ✋ | `document_id` | Invia il sollecito di firma. Limite: 1 ogni 24h per documento (429 con `Retry-After` se superato) |
| `read_heu_document` | `document_id`, `pages` (es. `"1-3"`, `"5"`, `"1,3,5-7"`), `layout`, `has_index`, `has_footer` | **Estrae il testo** del documento e lo restituisce in conversazione, senza salvare nulla su disco. Max 100 pagine se `pages` è omesso |
| `extract_heu_document_parties` | `document_id`, `pages`, `include_text` | **Dati delle parti**: combina i firmatari registrati con l'estrazione dal testo di codici fiscali, P.IVA, codice univoco SDI, email, PEC, luogo+data di nascita, indirizzi, CAP. Pattern ottimizzati per contratti italiani |
| `download_heu_document_pdf` | `document_id`, `layout` (codici UI: 100, 200-204, 210-214, 220-224, 230-234), `has_index`, `has_footer`, `output_path` | Genera e salva il PDF su disco; ritorna il path |

### 📎 PDF caricati

| Tool | Parametri | Cosa fa |
|---|---|---|
| `list_pdf_documents` | `type` (**richiesto**: document/template), `sort` | Lista PDF con stato (`to_sign`/`in_progress`/`signed`), tipo firma (FES/FEA), firmatari |
| `get_pdf_document` | `document_id` | Dettaglio: nome, stato, `signature_type`, date, firmatari con `has_read`/`has_signed` |
| `list_pdf_document_signers` | `document_id` | Firmatari del PDF: id, nome, email, ha letto, ha firmato |
| `list_pdf_document_signer_placeholders` | `document_id`, `signer_id` | Campi (firma/testo/checkbox) assegnati a un firmatario specifico, con posizione e stato di compilazione |
| `list_pdf_document_placeholders` | `document_id` | Tutti i campi del PDF |
| `create_pdf_document` ✋ | `source_document_id`, `email_subject`, `email_body`, `signers` (con `source_id`, `full_name`, `email`), `document_name`, `signature_type` (fes/fea), `placeholders` precompilabili | Crea un PDF firmabile **da un template esistente** e invia gli inviti. Con `fea` servono crediti sufficienti (422 altrimenti) |
| `prompt_pdf_document_signature` ✋ | `document_id` | Sollecito di firma per il PDF |
| `read_pdf_document` | `document_id`, `pages` | Estrae il testo del PDF (incluso quello **firmato**) e lo restituisce in conversazione |
| `extract_pdf_document_parties` | `document_id`, `pages`, `include_text` | Dati delle parti (come sopra) per i PDF caricati |
| `download_pdf_document` | `document_id`, `output_path` | Scarica il PDF — **versione firmata se disponibile** — e ritorna il path |
| `download_pdf_audit_trail` | `document_id`, `output_path` | Scarica l'**audit trail**: il registro PDF di chi ha letto/firmato e quando |
| `download_pdf_bundle` | `document_id`, `output_path` | Scarica lo **ZIP del fascicolo legale**: PDF firmato + audit trail + artefatti FES. Ideale per archiviazione a valore probatorio |
| `cancel_pdf_document` ✋ | `document_id` | **Annulla una richiesta di firma inviata**: il documento sparisce dagli elenchi e i link di firma vengono invalidati. Rifiutato con 409 se qualcuno ha già firmato |

### 🧩 Template PDF (gestione via API)

| Tool | Parametri | Cosa fa |
|---|---|---|
| `create_pdf_template` ✋ | `file_path` (PDF locale ≤ 5 MB), `document_name`, `signers` (`source_id`, `full_name`), `placeholders` (tipo, posizione %, pagina) | Crea un **template riutilizzabile** caricando un PDF dal computer. L'ID restituito si usa come `source_document_id` in `create_pdf_document` |
| `create_pdf_document_from_upload` ✋ | `file_path`, `document_name`, `email_subject`, `email_body`, `signers` (con email), `placeholders`, `signature_type` | **Scorciatoia completa**: carica un PDF e lo manda subito in firma, senza creare prima il template. Il documento nasce `to_sign` e i firmatari ricevono l'email immediatamente |
| `locate_pdf_text` | `file_path` **oppure** `document_id`, `search_terms` (default: parole chiave firma), `pages`, `include_all_lines` | **Trova le coordinate di testi nel PDF** (in %, origine in basso a sinistra — lo stesso sistema dei placeholder). È il tool che permette all'AI di posizionare i campi da sola: cerca "Firma", i nomi delle parti o qualsiasi ancora, e ottiene pagina + posizione di ognuna |
| `preview_pdf_template` | `document_id`, `output_path` | Scarica un'**anteprima annotata**: ogni campo è disegnato come riquadro etichettato con tipo e firmatario. Per verificare le posizioni prima dell'invio |
| `update_pdf_template` ✋ | `document_id`, `signers` (set completo sostitutivo), `placeholders` (idem; `[]` li cancella tutti), `document_name` | Sostituzione integrale di firmatari e campi di un template (l'ID resta invariato). Solo il proprietario |
| `delete_pdf_template` ✋ | `document_id` | Elimina (nasconde) il template da tutti gli elenchi |

✋ = il tool ha effetti verso l'esterno (crea, invia email, elimina): Claude chiede sempre conferma prima di eseguirlo.

#### Come si posizionano i placeholder

- `position_x` / `position_y`: **percentuale** della pagina (0–100 esclusi), origine **in basso a sinistra** (come nell'editor dell'app).
- `page_number`: parte da 1.
- Tipi: `signature`, `initials`, `text` (richiede `text_label`), `checkbox_optional`, `checkbox_required`.
- I firmatari si collegano ai campi tramite `source_id` → `signer_source_id`.
- I PDF ruotati (`/Rotate 90/180/270`) vengono rifiutati dall'API.

---

## Flussi di lavoro tipici

### 0. Invio in firma "intelligente": l'AI mappa i campi da sola

> *"Prendi `/Users/me/Desktop/Contratto.pdf`, trova dove devono firmare le parti e mandalo a cliente@example.com e fornitore@example.com."*

Cosa succede dietro le quinte:

1. **`locate_pdf_text`** analizza il PDF e trova le ancore: le righe "Firma del Cliente" / "Firma del Fornitore" (o i nomi delle parti) con le loro coordinate esatte in percentuale.
2. Claude **propone la mappatura**: *"Metto il campo firma del cliente a pagina 4, sopra l'etichetta 'Firma del Cliente' (x 17%, y 14%), e quello del fornitore accanto (x 50%, y 14%). Confermi?"*
3. Alla conferma, **`create_pdf_document_from_upload`** carica il PDF con i placeholder posizionati e invia le email di firma.
4. (Opzionale) **`preview_pdf_template`** per un controllo visivo se si è passati da un template.

Le coordinate restituite da `locate_pdf_text` sono già nel sistema dei placeholder HEU (percentuale, origine in basso a sinistra): nessuna conversione necessaria. Per layout complessi si può chiedere l'intera mappa della pagina con `include_all_lines=true`, o cercare termini specifici (`search_terms=["Il Committente", "Il Prestatore"]`).

⚠️ Limite: funziona sui PDF con testo. Le scansioni senza OCR non hanno testo estraibile — in quel caso indicare le posizioni manualmente.

### 1. Mandare in firma un PDF dal computer (tutto via chat)

> *"Prendi `/Users/me/Desktop/NDA.pdf` e mandalo in firma a Mario Rossi (mario@example.com). Campo firma in basso a destra dell'ultima pagina, oggetto email 'NDA da firmare'."*

Claude usa `create_pdf_document_from_upload` → il documento è creato e Mario riceve subito l'email. Poi:

> *"Mario ha firmato?"* → `get_pdf_document`
> *"Sollecitalo"* → `prompt_pdf_document_signature`
> *"È firmato, scaricami il fascicolo completo"* → `download_pdf_bundle`

### 2. Contratti ricorrenti con template

> *"Crea un template dal file `Contratto-tipo.pdf` con due firmatari: cliente e fornitore. Firma del cliente a pagina 3 in basso."* → `create_pdf_template`
> *"Fammi vedere l'anteprima per controllare le posizioni"* → `preview_pdf_template`
> *"Ora usalo per mandare il contratto ad ACME srl"* → `create_pdf_document`

### 3. Analisi documentale (l'AI legge i contratti)

> *"Riassumi il contratto `abc-123` e dimmi durata e condizioni di recesso"* → `read_heu_document`
> *"Confronta le clausole di responsabilità dei contratti X e Y"* → due `read_heu_document`
> *"Estrai i dati delle parti: ragione sociale, P.IVA, SDI, PEC"* → `extract_heu_document_parties`

L'estrazione parti è pensata per l'integrazione con flussi di **fatturazione elettronica italiana**: il codice univoco SDI e la P.IVA estratti dal contratto possono alimentare direttamente l'anagrafica del gestionale.

### 4. Monitoraggio e amministrazione

> *"Quali documenti di luglio non sono ancora stati firmati da tutti?"* → `list_heu_documents` con date + `have_editors_signed=false`
> *"Annulla la richiesta di firma del PDF 42, l'abbiamo mandata alla persona sbagliata"* → `cancel_pdf_document`

---

## Comportamenti e limiti da conoscere

| Cosa | Limite / comportamento |
|---|---|
| Rate limit API | **300 richieste / 5 minuti** (header `X-RateLimit-*` nelle risposte; 429 con `Retry-After` oltre soglia) |
| Solleciti firma | 1 ogni **24 ore** per documento |
| Upload PDF | Max **5 MB**, `application/pdf`, non ruotati |
| Lettura testo | Max **100 pagine** se `pages` non è specificato (il payload segnala `truncated: true`); PDF scansionati senza OCR non hanno testo estraibile |
| Firma FEA | Richiede **crediti FEA** disponibili per ogni firmatario (422 se insufficienti) |
| `list_heu_documents` con date | Passare **sempre entrambe** `created_from` e `created_to` |
| Annullamento PDF | Possibile **solo senza attività di firma** (409 altrimenti) |
| Download | I binari vengono salvati su disco (`HEU_DOWNLOAD_DIR`), mai trasmessi nel canale MCP |

## Stati dei documenti

| Stato | Significato |
|---|---|
| `to_sign` | In attesa di firme |
| `in_progress` | In preparazione/modifica |
| `in_review` | In revisione |
| `completed` | Flusso completato (non firmato) |
| `signed` | Completamente firmato |

## Sicurezza

- L'API key è letta **solo** da variabile d'ambiente: mai nel codice, mai nelle risposte, mai nei log.
- I tool con effetti esterni (✋) sono istruiti per richiedere sempre conferma esplicita all'utente.
- I file scaricati restano sul filesystem locale.

## Sviluppo

```bash
git clone https://github.com/heulegal/heu-mcp.git
cd heu-mcp
python3 -m venv venv
source venv/bin/activate
pip install -e .

# Avvio manuale per debug
HEU_API_KEY=... python server.py
```

La spec OpenAPI di riferimento è pubblicata su `https://api.heulegal.com/v1/specs/v1.yaml` (docs interattive: `https://api.heulegal.com/v1/docs`).

## Licenza

MIT — vedi [LICENSE](LICENSE).

## Link

- [HEU Legal](https://heulegal.com)
- [Documentazione API HEU](https://api.heulegal.com/v1/docs)
- [Model Context Protocol](https://modelcontextprotocol.io)
- [Pacchetto PyPI](https://pypi.org/project/heu-mcp/)
