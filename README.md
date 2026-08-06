<!-- mcp-name: io.github.Lucav21/heu-mcp -->

# HEU Legal MCP Server

[![PyPI](https://img.shields.io/pypi/v/heu-mcp.svg)](https://pypi.org/project/heu-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/heu-mcp.svg)](https://pypi.org/project/heu-mcp/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

MCP server (Model Context Protocol) per integrare l'[API HEU Legal](https://heulegal.com) con Claude (Claude Desktop, Claude Code, e altri client MCP). Gestisce documenti nativi HEU e PDF firmabili end-to-end via conversazione: creazione, condivisione per firma elettronica (valida in 180+ paesi), sollecito firmatari, download PDF.

## Funzionalità

27 tool che coprono l'intera API HEU v1 più lettura contenuto e estrazione strutturata delle parti:

**Documenti nativi HEU** — `list_heu_documents`, `get_heu_document`, `list_heu_document_placeholders`, `create_heu_document`, `prompt_heu_document_signature`, `extract_heu_document_parties`, `read_heu_document`, `download_heu_document_pdf`

**PDF caricati** — `list_pdf_documents`, `get_pdf_document`, `list_pdf_document_signers`, `list_pdf_document_signer_placeholders`, `list_pdf_document_placeholders`, `create_pdf_document`, `extract_pdf_document_parties`, `read_pdf_document`, `prompt_pdf_document_signature`, `download_pdf_document`, `download_pdf_audit_trail`, `download_pdf_bundle`, `cancel_pdf_document`

**Template PDF** — `create_pdf_template` (da file locale), `create_pdf_document_from_upload` (invio diretto da file), `preview_pdf_template`, `update_pdf_template`, `delete_pdf_template`

**Health** — `get_heu_health`

### Lettura del contenuto vs Download

Per ciascun tipo di documento esistono due modalità complementari:

| Tool | Cosa fa | Quando usarlo |
|------|---------|---------------|
| `read_heu_document` / `read_pdf_document` | Estrae il testo del PDF e lo restituisce direttamente nella conversazione | Quando vuoi che Claude **legga** il documento (riassumere, cercare clausole, confrontare) |
| `download_heu_document_pdf` | Salva il PDF su disco e ritorna il path | Quando ti serve il **file fisico** (archiviarlo, allegarlo a un'email, stamparlo) |

I tool di lettura accettano un parametro opzionale `pages` per leggere solo un range (es. `"1-3"`, `"5"`, `"1,3,5-7"`). Senza `pages` leggono fino a un massimo di 100 pagine.

## Requisiti

- Python ≥ 3.10
- Una API key HEU Legal (Profile → API Keys nella UI HEU; richiede subscription **Enterprise**)
- Almeno un **template** già creato sulla piattaforma HEU (documento nativo o PDF caricato)

## Installazione

### Da PyPI

```bash
pip install heu-mcp
```

### Da sorgenti

```bash
git clone https://github.com/Lucav21/heu-mcp.git
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

Se hai installato da sorgenti con venv:

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

## Variabili d'ambiente

| Variabile | Descrizione | Default |
|-----------|-------------|---------|
| `HEU_API_KEY` | API key HEU Legal (richiesto) | — |
| `HEU_BASE_URL` | URL base API | `https://api.heulegal.com/v1` |
| `HEU_DOWNLOAD_DIR` | Cartella dove salvare i PDF scaricati | `/tmp` |

## Esempi d'uso

Dopo aver configurato il MCP, chiedi a Claude:

- *"Lista i miei template HEU"*
- *"Mostrami i firmatari del PDF con id 26"*
- *"Sollecita la firma del documento 15c587e0-1715-45c6-bf72-24bcb86c0f90"*
- *"Riassumi il contratto con id ... e dimmi qual è la durata"* (usa `read_heu_document`)
- *"Confronta i contratti X e Y, segnalami le differenze nelle clausole"*
- *"Estrai i dati delle parti dal contratto ... — voglio nome, codice fiscale, P.IVA, codice univoco SDI"* (usa `extract_heu_document_parties`)
- *"Scarica il PDF del documento X con layout 220 e includi indice"*
- *"Crea un nuovo PDF firmabile dal template 56 per mario.rossi@example.com"*

## Note operative

- I tool che creano/inviano (`create_*`, `prompt_*`) chiedono sempre **conferma esplicita** all'utente prima dell'esecuzione.
- Rate limit dell'API: 300 richieste / 5 minuti. Gli header `X-RateLimit-*` sono restituiti nelle risposte.
- `prompt_*_signature` ha un limite di 1 sollecito ogni 24h per documento (HTTP 429 con `Retry-After`).
- `download_heu_document_pdf` salva il PDF in `HEU_DOWNLOAD_DIR` e restituisce il path: il binario non passa nel canale stdio.
- Per `create_pdf_document` con `signature_type=fea` serve avere credito FEA sufficiente per tutti i firmatari (altrimenti HTTP 422).

## Sicurezza

- Le API key non sono mai loggate.
- L'API key è letta solo da variabile d'ambiente: non viene mai scritta nel codice o nelle risposte.
- Il binario PDF è salvato sul filesystem locale, non viene trasmesso via canale MCP.

## Sviluppo

```bash
git clone https://github.com/Lucav21/heu-mcp.git
cd heu-mcp
python3 -m venv venv
source venv/bin/activate
pip install -e .

# Avvio manuale per debug
HEU_API_KEY=... python server.py
```

## Licenza

MIT — vedi [LICENSE](LICENSE).

## Link

- [HEU Legal](https://heulegal.com)
- [Documentazione API HEU](https://api.heulegal.com/v1)
- [Model Context Protocol](https://modelcontextprotocol.io)
