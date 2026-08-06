#!/usr/bin/env python3
"""HEU Legal API MCP Server - v0.1.0

MCP Server per integrare HEU Legal con Claude AI.
Gestisce documenti nativi HEU e PDF per firma elettronica via conversazione.

API docs: https://api.heulegal.com/v1
License: MIT
"""

import io
import json
import os
import re
import traceback
from pathlib import Path

import httpx
from pypdf import PdfReader

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

API_KEY = os.getenv("HEU_API_KEY", "")
BASE_URL = os.getenv("HEU_BASE_URL", "https://api.heulegal.com/v1").rstrip("/")
DOWNLOAD_DIR = Path(os.getenv("HEU_DOWNLOAD_DIR", "/tmp"))

DEFAULT_TIMEOUT = 30.0
DOWNLOAD_TIMEOUT = 120.0

app = Server("heu")


def _headers():
    return {
        "x-api-key": API_KEY,
        "Accept": "application/json",
    }


def _request(method: str, path: str, **kwargs):
    """Esegue una richiesta HTTP all'API HEU e ritorna (status, body_or_text, headers).

    Per risposte JSON ritorna il body parsato; per binari ritorna i bytes grezzi."""
    url = f"{BASE_URL}{path}"
    headers = kwargs.pop("headers", {})
    headers = {**_headers(), **headers}
    timeout = kwargs.pop("timeout", DEFAULT_TIMEOUT)
    with httpx.Client(timeout=timeout) as client:
        resp = client.request(method, url, headers=headers, **kwargs)
        ct = resp.headers.get("content-type", "")
        if "application/json" in ct:
            try:
                body = resp.json()
            except Exception:
                body = resp.text
        elif (
            "application/pdf" in ct
            or "application/zip" in ct
            or "application/octet-stream" in ct
            or resp.headers.get("content-disposition")
        ):
            body = resp.content
        else:
            try:
                body = resp.json()
            except Exception:
                body = resp.text
        return resp.status_code, body, dict(resp.headers)


def _ok(payload) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(payload, indent=2, ensure_ascii=False, default=str))]


def _err(status: int, body, hint: str | None = None) -> list[TextContent]:
    msg = {"error": True, "status": status, "body": body}
    if hint:
        msg["hint"] = hint
    return [TextContent(type="text", text=json.dumps(msg, indent=2, ensure_ascii=False, default=str))]


def _check_config() -> str | None:
    if not API_KEY:
        return "HEU_API_KEY non configurata. Imposta la variabile d'ambiente con la tua API key (Profile > API Keys nella UI HEU, richiede subscription Enterprise)."
    return None


def _safe_filename(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")
    return name or "document"


def _save_binary(content: bytes, output_path: str | None, default_name: str) -> Path:
    """Salva bytes su disco: usa output_path se fornito, altrimenti HEU_DOWNLOAD_DIR/default_name."""
    if output_path:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
    else:
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        out = DOWNLOAD_DIR / default_name
    out.write_bytes(content)
    return out


def _download_binary_to_disk(path: str, output_path: str | None, default_name: str, what: str) -> list[TextContent]:
    """GET binario dall'API e salvataggio su disco; ritorna path e dimensione."""
    status, body, headers = _request("GET", path, timeout=DOWNLOAD_TIMEOUT)
    if status != 200:
        return _err(status, body if not isinstance(body, (bytes, bytearray)) else body[:200].decode(errors="replace"))
    if not isinstance(body, (bytes, bytearray)):
        return _err(status, body, hint=f"Atteso {what} binario, ricevuto altro contenuto.")
    out = _save_binary(bytes(body), output_path, default_name)
    return _ok({
        "saved_to": str(out.resolve()),
        "size_bytes": len(body),
        "content_disposition": headers.get("content-disposition"),
    })


DEFAULT_MAX_PAGES = 100


# -------- Italian party-data extraction (regex-based) --------

_RE_FISCAL_CODE = re.compile(
    r"\b[A-Z]{6}[0-9LMNPQRSTUV]{2}[A-Z][0-9LMNPQRSTUV]{2}[A-Z][0-9LMNPQRSTUV]{3}[A-Z]\b"
)
_RE_VAT_LABELED = re.compile(
    r"(?:p\.?\s*iva|partita\s*iva|vat\s*number)[\s:.\-#nº°]*([0-9]{11})\b",
    re.IGNORECASE,
)
_RE_VAT_IT_PREFIX = re.compile(r"\bIT\s?([0-9]{11})\b")
_RE_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_RE_SDI_CODE = re.compile(
    r"(?:codice\s+(?:univoco|destinatario|sdi)(?:\s+(?:univoco|destinatario|sdi))?|c\.?u\.?\s*destinatario)[\s:.\-#]*([A-Z0-9]{7})\b",
    re.IGNORECASE,
)
_RE_PEC_LABELED = re.compile(
    r"(?:pec|posta\s*elettronica\s*certificata)[\s:.\-]*([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})",
    re.IGNORECASE,
)
_RE_BIRTH = re.compile(
    r"nat[oaie]\s+(?:a|in)?\s*([A-ZÀ-Ý][\wÀ-ÿ'\s.\-]{1,40}?)\s*(?:\(([A-Z]{2})\))?\s+il\s+("
    r"\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}"
    r"|\d{1,2}\s+(?:gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|agosto|settembre|ottobre|novembre|dicembre)\s+\d{2,4}"
    r")",
    re.IGNORECASE,
)
_RE_ADDRESS = re.compile(
    r"\b(?:via|viale|piazza|piazzale|corso|largo|vicolo|strada(?:\s+statale|\s+provinciale)?|loc(?:alit[àa])?\.?)\s+[A-ZÀ-Ý0-9][^,;\n\r]{2,90}",
    re.IGNORECASE,
)
_RE_CAP = re.compile(r"\b\d{5}\b")


def _norm_seq(values):
    seen = []
    out = []
    for v in values:
        key = v.upper() if isinstance(v, str) else v
        if key not in seen:
            seen.append(key)
            out.append(v)
    return out


def _extract_parties_from_text(text: str) -> dict:
    """Apply Italian regex patterns to extract structured party data from a contract."""
    if not text:
        return {
            "fiscal_codes": [],
            "vat_numbers": [],
            "sdi_codes": [],
            "emails": [],
            "pec_emails": [],
            "birth_info": [],
            "addresses": [],
            "postal_codes": [],
        }

    fiscal_codes = _norm_seq(m.group(0).upper() for m in _RE_FISCAL_CODE.finditer(text))

    vat_numbers: list[str] = []
    for m in _RE_VAT_LABELED.finditer(text):
        vat_numbers.append(m.group(1))
    for m in _RE_VAT_IT_PREFIX.finditer(text):
        vat_numbers.append(m.group(1))
    vat_numbers = _norm_seq(vat_numbers)

    sdi_codes = _norm_seq(m.group(1).upper() for m in _RE_SDI_CODE.finditer(text))

    emails = _norm_seq(m.group(0).lower() for m in _RE_EMAIL.finditer(text))
    pec_emails = _norm_seq(m.group(1).lower() for m in _RE_PEC_LABELED.finditer(text))

    birth_info = []
    for m in _RE_BIRTH.finditer(text):
        place = m.group(1).strip(" .,'\"") if m.group(1) else None
        province = m.group(2) or None
        date = m.group(3).strip() if m.group(3) else None
        entry = {"place": place, "province": province, "date": date}
        if entry not in birth_info:
            birth_info.append(entry)

    addresses = []
    for m in _RE_ADDRESS.finditer(text):
        candidate = m.group(0).strip(" ,;.")
        candidate = re.sub(r"\s+", " ", candidate)
        if candidate and candidate not in addresses:
            addresses.append(candidate)

    postal_codes = _norm_seq(m.group(0) for m in _RE_CAP.finditer(text))

    return {
        "fiscal_codes": fiscal_codes,
        "vat_numbers": vat_numbers,
        "sdi_codes": sdi_codes,
        "emails": emails,
        "pec_emails": pec_emails,
        "birth_info": birth_info,
        "addresses": addresses,
        "postal_codes": postal_codes,
    }


def _parse_pages_spec(spec: str, total_pages: int) -> list[int]:
    """Parse '1-3', '5', '1,3,5', '1-3,7' into a sorted list of page numbers (1-indexed).

    Out-of-range pages are silently clamped/dropped. Empty result raises ValueError."""
    if not spec or not spec.strip():
        raise ValueError("Empty pages spec")
    pages: set[int] = set()
    for raw in spec.split(","):
        part = raw.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            try:
                start = max(1, int(a.strip()))
                end = min(total_pages, int(b.strip()))
            except ValueError:
                raise ValueError(f"Invalid range '{part}'")
            if start <= end:
                pages.update(range(start, end + 1))
        else:
            try:
                n = int(part)
            except ValueError:
                raise ValueError(f"Invalid page number '{part}'")
            if 1 <= n <= total_pages:
                pages.add(n)
    if not pages:
        raise ValueError(f"No valid pages in spec '{spec}' (document has {total_pages} pages)")
    return sorted(pages)


def _extract_pdf_text(pdf_bytes: bytes, pages_spec: str | None, max_pages: int = DEFAULT_MAX_PAGES) -> dict:
    """Extract text from PDF bytes. Returns dict with text + metadata."""
    reader = PdfReader(io.BytesIO(pdf_bytes))
    total = len(reader.pages)

    if pages_spec:
        wanted = _parse_pages_spec(pages_spec, total)
        truncated = False
    else:
        if total > max_pages:
            wanted = list(range(1, max_pages + 1))
            truncated = True
        else:
            wanted = list(range(1, total + 1))
            truncated = False

    parts: list[str] = []
    for page_num in wanted:
        page = reader.pages[page_num - 1]
        try:
            text = page.extract_text() or ""
        except Exception as e:
            text = f"[error extracting text: {e}]"
        parts.append(f"--- Page {page_num} ---\n{text.strip()}")

    return {
        "text": "\n\n".join(parts),
        "pages_extracted": wanted,
        "pages_total": total,
        "truncated": truncated,
        "max_pages_default": max_pages,
    }


@app.list_tools()
async def list_tools():
    placeholders_kv_schema = {
        "type": "object",
        "description": "Mappa key->value dei placeholder da sostituire nel template (chiave = nome placeholder)",
        "additionalProperties": {"type": "string"},
    }

    pdf_signer_schema = {
        "type": "object",
        "properties": {
            "source_id": {"type": "string", "description": "ID del signer nel template sorgente"},
            "email": {"type": "string", "format": "email", "description": "Email del firmatario"},
            "full_name": {"type": "string", "description": "Nome completo del firmatario"},
        },
        "required": ["source_id", "email", "full_name"],
    }

    pdf_placeholder_schema = {
        "type": "object",
        "properties": {
            "source_id": {"type": "string", "description": "UUID del placeholder nel template"},
            "is_checked": {"type": ["boolean", "null"], "description": "Per checkbox: spuntato o no"},
            "text_value": {"type": ["string", "null"], "description": "Valore testuale da inserire"},
        },
        "required": ["source_id"],
    }

    template_signer_schema = {
        "type": "object",
        "properties": {
            "source_id": {"type": "string", "description": "Chiave scelta da te per collegare i placeholder a questo firmatario (es. 'signer-1'). Unica nella richiesta."},
            "full_name": {"type": "string", "description": "Nome completo del firmatario"},
        },
        "required": ["source_id", "full_name"],
    }

    template_placeholder_schema = {
        "type": "object",
        "properties": {
            "signer_source_id": {"type": "string", "description": "source_id del firmatario a cui appartiene il campo"},
            "type": {
                "type": "string",
                "enum": ["text", "signature", "initials", "checkbox_optional", "checkbox_required"],
                "description": "Tipo di campo",
            },
            "position_x": {"type": "number", "description": "Posizione orizzontale in % della larghezza pagina (0-100 esclusi), origine in basso a sinistra"},
            "position_y": {"type": "number", "description": "Posizione verticale in % dell'altezza pagina (0-100 esclusi), origine in basso a sinistra"},
            "page_number": {"type": "number", "description": "Numero pagina (parte da 1)"},
            "text_label": {"type": "string", "description": "Etichetta del campo. Richiesta solo per type=text."},
        },
        "required": ["signer_source_id", "type", "position_x", "position_y", "page_number"],
    }

    upload_signer_schema = {
        "type": "object",
        "properties": {
            "source_id": {"type": "string", "description": "Chiave scelta da te per collegare i placeholder a questo firmatario (es. 'signer-1')"},
            "full_name": {"type": "string", "description": "Nome completo del firmatario"},
            "email": {"type": "string", "format": "email", "description": "Email del firmatario (riceverà l'invito a firmare)"},
        },
        "required": ["source_id", "full_name", "email"],
    }

    upload_placeholder_schema = {
        "type": "object",
        "properties": {
            "signer_source_id": {"type": "string", "description": "source_id del firmatario a cui appartiene il campo"},
            "type": {
                "type": "string",
                "enum": ["text", "signature", "initials", "checkbox_optional", "checkbox_required"],
                "description": "Tipo di campo",
            },
            "position_x": {"type": "number", "description": "Posizione orizzontale in % (0-100 esclusi), origine in basso a sinistra"},
            "position_y": {"type": "number", "description": "Posizione verticale in % (0-100 esclusi), origine in basso a sinistra"},
            "page_number": {"type": "number", "description": "Numero pagina (parte da 1)"},
            "text_label": {"type": "string", "description": "Etichetta (per type=text)"},
            "text_value": {"type": "string", "description": "Valore precompilato (opzionale)"},
            "is_checked": {"type": "boolean", "description": "Per checkbox: precompilato spuntato (opzionale)"},
        },
        "required": ["signer_source_id", "type", "position_x", "position_y", "page_number"],
    }

    return [
        Tool(
            name="get_heu_health",
            description="Health check API HEU. Ritorna { message: 'ok', status: 200 } se operativa.",
            inputSchema={"type": "object", "properties": {}},
        ),
        # ---------- DOCUMENTS (native HEU) ----------
        Tool(
            name="list_heu_documents",
            description=(
                "Lista documenti/template nativi HEU con filtri opzionali. "
                "IMPORTANTE: quando filtri per intervallo di date passa SEMPRE entrambi i parametri "
                "'created_from' e 'created_to' insieme, altrimenti l'API potrebbe non restituire "
                "tutti i documenti del periodo. Se vuoi tutti i documenti senza limiti di data, "
                "ometti entrambi."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["document", "template"], "description": "Filtra per tipo"},
                    "sort": {"type": "string", "enum": ["asc", "desc"], "description": "Ordinamento per data"},
                    "created_from": {
                        "type": "string",
                        "description": (
                            "Data inizio del filtro (ISO 8601, es. 2025-01-01T00:00:00Z). "
                            "USA SEMPRE INSIEME a 'created_to': passare solo una delle due date "
                            "può far escludere documenti dal risultato."
                        ),
                    },
                    "created_to": {
                        "type": "string",
                        "description": (
                            "Data fine del filtro (ISO 8601, es. 2025-01-31T23:59:59Z). "
                            "USA SEMPRE INSIEME a 'created_from'."
                        ),
                    },
                    "have_editors_signed": {"type": "boolean", "description": "Filtra per: tutti gli editor hanno firmato"},
                },
            },
        ),
        Tool(
            name="get_heu_document",
            description="Dettaglio di un documento o template HEU per ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "string", "description": "ID del documento (UUID per document/template HEU)"},
                },
                "required": ["document_id"],
            },
        ),
        Tool(
            name="list_heu_document_placeholders",
            description="Lista i placeholder di un documento/template HEU (chiavi sostituibili nel testo).",
            inputSchema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "string", "description": "ID del documento HEU"},
                },
                "required": ["document_id"],
            },
        ),
        Tool(
            name="create_heu_document",
            description=(
                "Crea e condivide un nuovo documento HEU partendo da un template esistente. "
                "Invia email ai destinatari. IMPORTANTE: chiedere conferma all'utente prima di eseguire."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_document_id": {"type": "string", "description": "ID del template sorgente"},
                    "document_name": {"type": "string", "description": "Nome del nuovo documento"},
                    "document_type": {"type": "string", "enum": ["document", "template"], "description": "Default: document"},
                    "email_subject": {"type": "string", "description": "Oggetto email di condivisione"},
                    "email_text": {"type": "string", "description": "Corpo email di condivisione"},
                    "email_to": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Lista email destinatari (oppure stringa singola)",
                    },
                    "placeholders": placeholders_kv_schema,
                },
                "required": ["source_document_id", "email_subject", "email_text", "email_to"],
            },
        ),
        Tool(
            name="prompt_heu_document_signature",
            description=(
                "Invia un sollecito di firma per un documento HEU. "
                "Limite: 1 prompt ogni 24h per documento (altrimenti 429). "
                "IMPORTANTE: chiedere conferma all'utente prima di eseguire."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "string", "description": "ID del documento HEU"},
                },
                "required": ["document_id"],
            },
        ),
        Tool(
            name="extract_heu_document_parties",
            description=(
                "Estrae i dati anagrafici delle parti da un documento HEU. "
                "Combina i metadati registrati (firmatari, ruoli, stato firma) con dati estratti "
                "dal testo del contratto: codice fiscale, P.IVA, codice univoco SDI, email/PEC, "
                "luogo e data di nascita, indirizzi, CAP. "
                "Pattern ottimizzati per contratti italiani."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "string", "description": "ID del documento HEU"},
                    "pages": {
                        "type": "string",
                        "description": "Range pagine: '1-3', '5', '1,3,5-7'. Default: tutte (max 100). Le 'parti' sono spesso in pagina 1-2.",
                    },
                    "include_text": {
                        "type": "boolean",
                        "description": "Se true include nel risultato anche il testo grezzo (utile per ulteriore analisi). Default: false.",
                    },
                },
                "required": ["document_id"],
            },
        ),
        Tool(
            name="read_heu_document",
            description=(
                "Legge il contenuto testuale di un documento HEU senza salvarlo su disco. "
                "Scarica il PDF dall'API HEU, ne estrae il testo e lo restituisce direttamente "
                "nella risposta — utile per riassumere, cercare clausole, confrontare contratti. "
                "Default: tutte le pagine fino a un limite di 100. Per documenti più lunghi usa il parametro 'pages'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "string", "description": "ID del documento HEU"},
                    "pages": {
                        "type": "string",
                        "description": "Range pagine: '1-3', '5', '1,3,5-7'. Default: tutte (max 100).",
                    },
                    "layout": {
                        "type": "string",
                        "enum": [
                            "100", "200", "201", "202", "203", "204",
                            "210", "211", "212", "213", "214",
                            "220", "221", "222", "223", "224",
                            "230", "231", "232", "233", "234",
                        ],
                        "description": "Codice layout (opzionale)",
                    },
                    "has_index": {"type": "boolean", "description": "Includi indice (opzionale)"},
                    "has_footer": {"type": "boolean", "description": "Includi footer (opzionale)"},
                },
                "required": ["document_id"],
            },
        ),
        Tool(
            name="download_heu_document_pdf",
            description=(
                "Scarica il PDF di un documento HEU e lo salva localmente. Ritorna il path del file. "
                "Layout opzionale (codici a 3 cifre della UI HEU)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "string", "description": "ID del documento HEU"},
                    "layout": {
                        "type": "string",
                        "enum": [
                            "100", "200", "201", "202", "203", "204",
                            "210", "211", "212", "213", "214",
                            "220", "221", "222", "223", "224",
                            "230", "231", "232", "233", "234",
                        ],
                        "description": "Codice layout (opzionale)",
                    },
                    "has_index": {"type": "boolean", "description": "Includi indice (opzionale)"},
                    "has_footer": {"type": "boolean", "description": "Includi footer (opzionale)"},
                    "output_path": {"type": "string", "description": "Path output personalizzato (opzionale, default: HEU_DOWNLOAD_DIR/heu_<id>.pdf)"},
                },
                "required": ["document_id"],
            },
        ),
        # ---------- PDFs (uploaded) ----------
        Tool(
            name="list_pdf_documents",
            description="Lista PDF documenti/template caricati su HEU.",
            inputSchema={
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["document", "template"], "description": "Tipo (richiesto)"},
                    "sort": {"type": "string", "enum": ["asc", "desc"], "description": "Ordinamento"},
                },
                "required": ["type"],
            },
        ),
        Tool(
            name="get_pdf_document",
            description="Dettaglio di un PDF documento/template HEU per ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "string", "description": "ID del PDF"},
                },
                "required": ["document_id"],
            },
        ),
        Tool(
            name="list_pdf_document_signers",
            description="Lista i firmatari di un PDF.",
            inputSchema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "string", "description": "ID del PDF"},
                },
                "required": ["document_id"],
            },
        ),
        Tool(
            name="list_pdf_document_signer_placeholders",
            description="Lista i placeholder/campi di firma di un signer specifico su un PDF.",
            inputSchema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "string", "description": "ID del PDF"},
                    "signer_id": {"type": "string", "description": "ID del firmatario"},
                },
                "required": ["document_id", "signer_id"],
            },
        ),
        Tool(
            name="list_pdf_document_placeholders",
            description="Lista tutti i placeholder/campi di firma di un PDF.",
            inputSchema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "string", "description": "ID del PDF"},
                },
                "required": ["document_id"],
            },
        ),
        Tool(
            name="create_pdf_document",
            description=(
                "Crea e condivide un nuovo PDF firmabile partendo da un template PDF esistente. "
                "Richiede signers e (opzionalmente) i valori dei placeholder. "
                "Con signature_type='fea' serve avere credito FEA sufficiente. "
                "IMPORTANTE: chiedere conferma all'utente prima di eseguire."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_document_id": {"type": "string", "description": "ID del template PDF sorgente"},
                    "document_name": {"type": "string", "description": "Nome del nuovo documento"},
                    "signature_type": {"type": "string", "enum": ["fes", "fea"], "description": "Tipo firma. Default: fes"},
                    "email_subject": {"type": "string", "description": "Oggetto email di condivisione"},
                    "email_body": {"type": "string", "description": "Corpo email di condivisione"},
                    "signers": {
                        "type": "array",
                        "items": pdf_signer_schema,
                        "description": "Lista firmatari (richiesto)",
                    },
                    "placeholders": {
                        "type": "array",
                        "items": pdf_placeholder_schema,
                        "description": "Valori dei placeholder (opzionale)",
                    },
                },
                "required": ["source_document_id", "email_subject", "email_body", "signers"],
            },
        ),
        Tool(
            name="extract_pdf_document_parties",
            description=(
                "Estrae i dati anagrafici delle parti da un PDF caricato. "
                "Combina i metadati dei firmatari registrati con dati estratti dal testo: "
                "codice fiscale, P.IVA, codice univoco SDI, email/PEC, luogo e data di nascita, indirizzi, CAP. "
                "Pattern ottimizzati per contratti italiani."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "string", "description": "ID del PDF caricato"},
                    "pages": {
                        "type": "string",
                        "description": "Range pagine: '1-3', '5', '1,3,5-7'. Default: tutte (max 100).",
                    },
                    "include_text": {
                        "type": "boolean",
                        "description": "Se true include nel risultato anche il testo grezzo. Default: false.",
                    },
                },
                "required": ["document_id"],
            },
        ),
        Tool(
            name="read_pdf_document",
            description=(
                "Legge il contenuto testuale di un PDF caricato senza salvarlo su disco. "
                "Scarica il PDF (incluso quello firmato, se disponibile), ne estrae il testo "
                "e lo restituisce direttamente nella risposta. "
                "Default: tutte le pagine fino a un limite di 100."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "string", "description": "ID del PDF caricato"},
                    "pages": {
                        "type": "string",
                        "description": "Range pagine: '1-3', '5', '1,3,5-7'. Default: tutte (max 100).",
                    },
                },
                "required": ["document_id"],
            },
        ),
        Tool(
            name="prompt_pdf_document_signature",
            description=(
                "Invia un sollecito di firma per un PDF. "
                "IMPORTANTE: chiedere conferma all'utente prima di eseguire."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "string", "description": "ID del PDF"},
                },
                "required": ["document_id"],
            },
        ),
        Tool(
            name="download_pdf_document",
            description=(
                "Scarica il PDF di un documento caricato (versione firmata se disponibile) "
                "e lo salva localmente. Ritorna il path del file."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "string", "description": "ID del PDF"},
                    "output_path": {"type": "string", "description": "Path output personalizzato (opzionale, default: HEU_DOWNLOAD_DIR/heu_pdf_<id>.pdf)"},
                },
                "required": ["document_id"],
            },
        ),
        Tool(
            name="download_pdf_audit_trail",
            description=(
                "Scarica l'audit trail (registro delle attività di firma) di un PDF "
                "e lo salva localmente. Ritorna il path del file."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "string", "description": "ID del PDF"},
                    "output_path": {"type": "string", "description": "Path output personalizzato (opzionale)"},
                },
                "required": ["document_id"],
            },
        ),
        Tool(
            name="download_pdf_bundle",
            description=(
                "Scarica il bundle ZIP completo di un PDF (documento + audit trail + artefatti FES) "
                "e lo salva localmente. Ritorna il path del file. Utile per archiviazione legale."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "string", "description": "ID del PDF"},
                    "output_path": {"type": "string", "description": "Path output personalizzato (opzionale)"},
                },
                "required": ["document_id"],
            },
        ),
        Tool(
            name="create_pdf_template",
            description=(
                "Crea un template PDF riutilizzabile caricando un file PDF locale (max 5 MB) "
                "con firmatari e placeholder. Ritorna l'ID del nuovo template, utilizzabile come "
                "source_document_id in create_pdf_document. Le posizioni dei placeholder sono in "
                "percentuale (0-100) con origine in basso a sinistra. PDF ruotati vengono rifiutati. "
                "IMPORTANTE: chiedere conferma all'utente prima di eseguire."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Path locale del file PDF da caricare (max 5 MB)"},
                    "document_name": {"type": "string", "description": "Nome del template"},
                    "signers": {"type": "array", "items": template_signer_schema, "description": "Firmatari del template (senza email)"},
                    "placeholders": {"type": "array", "items": template_placeholder_schema, "description": "Campi firma/testo/checkbox posizionati sul PDF"},
                },
                "required": ["file_path", "document_name", "signers", "placeholders"],
            },
        ),
        Tool(
            name="create_pdf_document_from_upload",
            description=(
                "Crea e invia direttamente un PDF firmabile caricando un file locale (max 5 MB), "
                "senza passare da un template. Richiede firmatari completi di email, oggetto/corpo "
                "email e layout placeholder (opzionalmente precompilati). Il documento viene creato "
                "in stato 'to_sign' e i firmatari ricevono subito l'email. "
                "Con signature_type='fea' servono crediti FEA sufficienti. "
                "IMPORTANTE: chiedere conferma all'utente prima di eseguire."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Path locale del file PDF da caricare (max 5 MB)"},
                    "document_name": {"type": "string", "description": "Nome del documento"},
                    "signature_type": {"type": "string", "enum": ["fes", "fea"], "description": "Tipo firma. Default: fes"},
                    "email_subject": {"type": "string", "description": "Oggetto email di invito alla firma"},
                    "email_body": {"type": "string", "description": "Corpo email di invito alla firma"},
                    "signers": {"type": "array", "items": upload_signer_schema, "description": "Firmatari (con email)"},
                    "placeholders": {"type": "array", "items": upload_placeholder_schema, "description": "Campi posizionati sul PDF, opzionalmente precompilati"},
                },
                "required": ["file_path", "document_name", "email_subject", "email_body", "signers", "placeholders"],
            },
        ),
        Tool(
            name="preview_pdf_template",
            description=(
                "Scarica un'anteprima annotata di un template PDF: ogni placeholder è disegnato "
                "come un riquadro etichettato con tipo e firmatario. Utile per verificare il "
                "posizionamento dei campi prima di usare il template. Salva il PDF localmente e "
                "ritorna il path. Solo il proprietario del template può usarlo."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "string", "description": "ID del template PDF"},
                    "output_path": {"type": "string", "description": "Path output personalizzato (opzionale)"},
                },
                "required": ["document_id"],
            },
        ),
        Tool(
            name="update_pdf_template",
            description=(
                "Sostituisce integralmente firmatari e placeholder di un template PDF esistente "
                "(l'ID template resta lo stesso). Solo il proprietario; il documento deve essere "
                "di tipo 'template'. Placeholder omessi o [] cancella tutti i campi. "
                "IMPORTANTE: chiedere conferma all'utente prima di eseguire."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "string", "description": "ID del template PDF"},
                    "document_name": {"type": "string", "description": "Nuovo nome del template (opzionale, aggiornato solo se fornito)"},
                    "signers": {"type": "array", "items": template_signer_schema, "description": "Set completo sostitutivo dei firmatari (richiesto)"},
                    "placeholders": {"type": "array", "items": template_placeholder_schema, "description": "Set completo sostitutivo dei placeholder (ometti o [] per cancellarli tutti)"},
                },
                "required": ["document_id", "signers"],
            },
        ),
        Tool(
            name="delete_pdf_template",
            description=(
                "Elimina (nasconde) un template PDF da tutti gli elenchi. I template non vengono "
                "mai firmati quindi l'eliminazione è sempre consentita. "
                "IMPORTANTE: chiedere SEMPRE conferma esplicita all'utente prima di eseguire."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "string", "description": "ID del template PDF da eliminare"},
                },
                "required": ["document_id"],
            },
        ),
        Tool(
            name="cancel_pdf_document",
            description=(
                "Annulla una richiesta di firma inviata: nasconde il PDF da tutti gli elenchi e "
                "invalida l'accesso alla firma, così i firmatari in attesa non possono più firmare. "
                "Viene rifiutato con 409 se il documento ha già attività di firma. "
                "IMPORTANTE: chiedere SEMPRE conferma esplicita all'utente prima di eseguire."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "string", "description": "ID del documento PDF da annullare"},
                },
                "required": ["document_id"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    cfg_err = _check_config()
    if cfg_err and name != "get_heu_health":
        return [TextContent(type="text", text=cfg_err)]

    try:
        if name == "get_heu_health":
            if not API_KEY:
                return [TextContent(type="text", text="HEU_API_KEY non configurata.")]
            status, body, _ = _request("GET", "/health")
            return _ok(body) if status == 200 else _err(status, body)

        # ---------- DOCUMENTS ----------
        if name == "list_heu_documents":
            params = {}
            for k in ("type", "sort", "created_from", "created_to"):
                if k in arguments and arguments[k] is not None:
                    params[k] = arguments[k]
            if "have_editors_signed" in arguments and arguments["have_editors_signed"] is not None:
                params["have_editors_signed"] = "true" if arguments["have_editors_signed"] else "false"
            status, body, _ = _request("GET", "/documents", params=params)
            return _ok(body) if status == 200 else _err(status, body)

        if name == "get_heu_document":
            doc_id = arguments["document_id"]
            status, body, _ = _request("GET", f"/documents/{doc_id}")
            return _ok(body) if status == 200 else _err(status, body)

        if name == "list_heu_document_placeholders":
            doc_id = arguments["document_id"]
            status, body, _ = _request("GET", f"/documents/{doc_id}/placeholders")
            return _ok(body) if status == 200 else _err(status, body)

        if name == "create_heu_document":
            to_addr = arguments["email_to"]
            if isinstance(to_addr, list) and len(to_addr) == 1:
                to_addr = to_addr[0]
            payload = {
                "source_document_id": arguments["source_document_id"],
                "email": {
                    "subject": arguments["email_subject"],
                    "text": arguments["email_text"],
                    "to_addresses": to_addr,
                },
            }
            if "document_name" in arguments:
                payload["document_name"] = arguments["document_name"]
            if "document_type" in arguments:
                payload["document_type"] = arguments["document_type"]
            if "placeholders" in arguments and arguments["placeholders"]:
                payload["placeholders"] = arguments["placeholders"]
            status, body, _ = _request("POST", "/documents", json=payload)
            return _ok(body) if status in (200, 201) else _err(status, body)

        if name == "prompt_heu_document_signature":
            doc_id = arguments["document_id"]
            status, body, headers = _request("POST", f"/documents/{doc_id}/signatures/prompts")
            if status in (200, 201):
                return _ok(body)
            hint = None
            if status == 429:
                retry = headers.get("retry-after")
                hint = f"Sollecito già inviato di recente. Riprova tra {retry}s." if retry else "Sollecito già inviato di recente. Limite 1/24h."
            return _err(status, body, hint=hint)

        if name == "extract_heu_document_parties":
            doc_id = arguments["document_id"]
            include_text = bool(arguments.get("include_text"))

            # 1) Document metadata (registered parties: members, owner, editors, status)
            meta_status, meta_body, _h = _request("GET", f"/documents/{doc_id}")
            registered_parties = []
            doc_metadata = None
            if meta_status == 200 and isinstance(meta_body, dict):
                data = meta_body.get("data", meta_body) or {}
                doc_metadata = {
                    "name": data.get("name"),
                    "status": data.get("status"),
                    "owner": data.get("owner"),
                    "created": data.get("created"),
                    "updated": data.get("updated"),
                }
                for member in (data.get("members") or []):
                    registered_parties.append({
                        "source": "members",
                        "email": member.get("email"),
                        "role": member.get("role"),
                        "has_signed": member.get("has_signed"),
                        "signed_at": member.get("signed_at"),
                    })
                for editor in (data.get("editors") or []):
                    if not any(p.get("email") == editor for p in registered_parties):
                        registered_parties.append({"source": "editors", "email": editor, "role": "editor"})

            # 2) Document body text
            status, body, _h = _request("POST", f"/documents/{doc_id}/download", timeout=DOWNLOAD_TIMEOUT)
            if status != 200 or not isinstance(body, (bytes, bytearray)):
                return _err(status, body, hint="Impossibile scaricare il PDF per l'estrazione testo.")
            try:
                pdf_result = _extract_pdf_text(bytes(body), arguments.get("pages"))
            except ValueError as e:
                return _err(400, str(e), hint="Errore nel parametro 'pages'.")
            except Exception as e:
                return _err(500, f"Errore estrazione testo: {e}")

            extracted = _extract_parties_from_text(pdf_result["text"])

            payload = {
                "document_id": doc_id,
                "document_metadata": doc_metadata,
                "registered_parties": registered_parties,
                "extracted_from_text": extracted,
                "pages_total": pdf_result["pages_total"],
                "pages_extracted": pdf_result["pages_extracted"],
                "truncated": pdf_result["truncated"],
            }
            if include_text:
                payload["raw_text"] = pdf_result["text"]
            return _ok(payload)

        if name == "extract_pdf_document_parties":
            doc_id = arguments["document_id"]
            include_text = bool(arguments.get("include_text"))

            # 1) PDF metadata + signers
            doc_metadata = None
            registered_parties = []
            meta_status, meta_body, _h = _request("GET", f"/pdfs/{doc_id}")
            if meta_status == 200 and isinstance(meta_body, dict):
                data = meta_body.get("data", meta_body) or {}
                doc_metadata = {
                    "name": data.get("name"),
                    "status": data.get("status"),
                    "signature_type": data.get("signature_type"),
                    "created_at": data.get("created_at"),
                    "updated_at": data.get("updated_at"),
                }

            sgn_status, sgn_body, _h = _request("GET", f"/pdfs/{doc_id}/signers")
            if sgn_status == 200 and isinstance(sgn_body, dict):
                for s in (sgn_body.get("data") or []):
                    registered_parties.append({
                        "source": "signers",
                        "id": s.get("id"),
                        "full_name": s.get("full_name"),
                        "email": s.get("email"),
                        "has_read": s.get("has_read"),
                        "has_signed": s.get("has_signed"),
                    })

            # 2) PDF body text via the native pdfs/{id}/download endpoint
            status, body, _h = _request("GET", f"/pdfs/{doc_id}/download", timeout=DOWNLOAD_TIMEOUT)
            extracted = None
            text_warning = None
            pdf_result = None
            if status == 200 and isinstance(body, (bytes, bytearray)):
                try:
                    pdf_result = _extract_pdf_text(bytes(body), arguments.get("pages"))
                    extracted = _extract_parties_from_text(pdf_result["text"])
                except ValueError as e:
                    return _err(400, str(e), hint="Errore nel parametro 'pages'.")
                except Exception as e:
                    text_warning = f"Errore estrazione testo: {e}"
            else:
                text_warning = (
                    f"Download del contenuto del PDF fallito (HTTP {status}). "
                    "Restituiti solo i metadati."
                )

            payload = {
                "document_id": doc_id,
                "document_metadata": doc_metadata,
                "registered_parties": registered_parties,
                "extracted_from_text": extracted,
            }
            if pdf_result:
                payload["pages_total"] = pdf_result["pages_total"]
                payload["pages_extracted"] = pdf_result["pages_extracted"]
                payload["truncated"] = pdf_result["truncated"]
                if include_text:
                    payload["raw_text"] = pdf_result["text"]
            if text_warning:
                payload["text_extraction_warning"] = text_warning
            return _ok(payload)

        if name == "read_heu_document":
            doc_id = arguments["document_id"]
            payload = {}
            for k in ("layout", "has_index", "has_footer"):
                if k in arguments and arguments[k] is not None:
                    payload[k] = arguments[k]
            kwargs = {"timeout": DOWNLOAD_TIMEOUT}
            if payload:
                kwargs["json"] = payload
            status, body, _headers = _request("POST", f"/documents/{doc_id}/download", **kwargs)
            if status != 200:
                return _err(status, body)
            if not isinstance(body, (bytes, bytearray)):
                return _err(status, body, hint="Atteso PDF binario, ricevuto altro contenuto.")
            try:
                result = _extract_pdf_text(bytes(body), arguments.get("pages"))
            except ValueError as e:
                return _err(400, str(e), hint="Errore nel parametro 'pages'.")
            except Exception as e:
                return _err(500, f"Errore estrazione testo: {e}")
            return _ok({
                "document_id": doc_id,
                "pages_total": result["pages_total"],
                "pages_extracted": result["pages_extracted"],
                "truncated": result["truncated"],
                "note": (
                    f"Mostrate solo le prime {result['max_pages_default']} pagine. "
                    "Usa il parametro 'pages' per leggere oltre questo limite."
                ) if result["truncated"] else None,
                "text": result["text"],
            })

        if name == "read_pdf_document":
            doc_id = arguments["document_id"]
            status, body, _headers = _request(
                "GET", f"/pdfs/{doc_id}/download", timeout=DOWNLOAD_TIMEOUT
            )
            if status != 200:
                return _err(status, body)
            if not isinstance(body, (bytes, bytearray)):
                return _err(status, body, hint="Atteso PDF binario, ricevuto altro contenuto.")
            try:
                result = _extract_pdf_text(bytes(body), arguments.get("pages"))
            except ValueError as e:
                return _err(400, str(e), hint="Errore nel parametro 'pages'.")
            except Exception as e:
                return _err(500, f"Errore estrazione testo: {e}")
            return _ok({
                "document_id": doc_id,
                "pages_total": result["pages_total"],
                "pages_extracted": result["pages_extracted"],
                "truncated": result["truncated"],
                "note": (
                    f"Mostrate solo le prime {result['max_pages_default']} pagine. "
                    "Usa il parametro 'pages' per leggere oltre questo limite."
                ) if result["truncated"] else None,
                "text": result["text"],
            })

        if name == "download_heu_document_pdf":
            doc_id = arguments["document_id"]
            payload = {}
            for k in ("layout", "has_index", "has_footer"):
                if k in arguments and arguments[k] is not None:
                    payload[k] = arguments[k]
            kwargs = {"timeout": DOWNLOAD_TIMEOUT}
            if payload:
                kwargs["json"] = payload
            status, body, headers = _request("POST", f"/documents/{doc_id}/download", **kwargs)
            if status != 200:
                return _err(status, body)
            if not isinstance(body, (bytes, bytearray)):
                return _err(status, body, hint="Atteso PDF binario, ricevuto altro contenuto.")
            output_path = arguments.get("output_path")
            if output_path:
                out = Path(output_path)
            else:
                DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
                out = DOWNLOAD_DIR / f"heu_{_safe_filename(doc_id)}.pdf"
            out.write_bytes(body)
            return _ok({
                "saved_to": str(out.resolve()),
                "size_bytes": len(body),
                "content_disposition": headers.get("content-disposition"),
            })

        # ---------- PDFs ----------
        if name == "list_pdf_documents":
            params = {"type": arguments["type"]}
            if "sort" in arguments and arguments["sort"]:
                params["sort"] = arguments["sort"]
            status, body, _ = _request("GET", "/pdfs", params=params)
            return _ok(body) if status == 200 else _err(status, body)

        if name == "get_pdf_document":
            doc_id = arguments["document_id"]
            status, body, _ = _request("GET", f"/pdfs/{doc_id}")
            return _ok(body) if status == 200 else _err(status, body)

        if name == "list_pdf_document_signers":
            doc_id = arguments["document_id"]
            status, body, _ = _request("GET", f"/pdfs/{doc_id}/signers")
            return _ok(body) if status == 200 else _err(status, body)

        if name == "list_pdf_document_signer_placeholders":
            doc_id = arguments["document_id"]
            signer_id = arguments["signer_id"]
            status, body, _ = _request("GET", f"/pdfs/{doc_id}/signers/{signer_id}/placeholders")
            return _ok(body) if status == 200 else _err(status, body)

        if name == "list_pdf_document_placeholders":
            doc_id = arguments["document_id"]
            status, body, _ = _request("GET", f"/pdfs/{doc_id}/placeholders")
            return _ok(body) if status == 200 else _err(status, body)

        if name == "create_pdf_document":
            payload = {
                "source_document_id": arguments["source_document_id"],
                "email": {
                    "subject": arguments["email_subject"],
                    "body": arguments["email_body"],
                },
                "signers": arguments["signers"],
            }
            if "document_name" in arguments:
                payload["document_name"] = arguments["document_name"]
            if "signature_type" in arguments:
                payload["signature_type"] = arguments["signature_type"]
            if "placeholders" in arguments and arguments["placeholders"]:
                payload["placeholders"] = arguments["placeholders"]
            status, body, _ = _request("POST", "/pdfs", json=payload)
            if status in (200, 201):
                return _ok(body)
            hint = None
            if status == 422:
                hint = "Crediti FEA insufficienti per il numero di firmatari."
            return _err(status, body, hint=hint)

        if name == "prompt_pdf_document_signature":
            doc_id = arguments["document_id"]
            status, body, _ = _request("POST", f"/pdfs/{doc_id}/signatures/prompts")
            return _ok(body) if status in (200, 201) else _err(status, body)

        if name == "download_pdf_document":
            doc_id = arguments["document_id"]
            return _download_binary_to_disk(
                f"/pdfs/{doc_id}/download",
                arguments.get("output_path"),
                f"heu_pdf_{_safe_filename(doc_id)}.pdf",
                "PDF",
            )

        if name == "download_pdf_audit_trail":
            doc_id = arguments["document_id"]
            return _download_binary_to_disk(
                f"/pdfs/{doc_id}/audit-trail",
                arguments.get("output_path"),
                f"heu_pdf_{_safe_filename(doc_id)}_audit_trail.pdf",
                "PDF audit trail",
            )

        if name == "download_pdf_bundle":
            doc_id = arguments["document_id"]
            return _download_binary_to_disk(
                f"/pdfs/{doc_id}/bundle",
                arguments.get("output_path"),
                f"heu_pdf_{_safe_filename(doc_id)}_bundle.zip",
                "bundle ZIP",
            )

        if name == "create_pdf_template":
            file_path = Path(arguments["file_path"])
            if not file_path.is_file():
                return _err(400, f"File non trovato: {file_path}")
            if file_path.stat().st_size > 5 * 1024 * 1024:
                return _err(400, f"File troppo grande ({file_path.stat().st_size} byte): il limite è 5 MB.")
            data_payload = {
                "documentName": arguments["document_name"],
                "signers": arguments["signers"],
                "placeholders": arguments["placeholders"],
            }
            status, body, _ = _request(
                "POST",
                "/pdfs/templates",
                files={"file": (file_path.name, file_path.read_bytes(), "application/pdf")},
                data={"data": json.dumps(data_payload, ensure_ascii=False)},
                timeout=DOWNLOAD_TIMEOUT,
            )
            if status in (200, 201):
                return _ok(body)
            hint = None
            if status == 400:
                hint = "Verifica che il PDF non sia ruotato (rifiutato) e che i placeholder siano validi (posizioni 0-100 esclusi, signer_source_id corrispondenti)."
            return _err(status, body, hint=hint)

        if name == "create_pdf_document_from_upload":
            file_path = Path(arguments["file_path"])
            if not file_path.is_file():
                return _err(400, f"File non trovato: {file_path}")
            if file_path.stat().st_size > 5 * 1024 * 1024:
                return _err(400, f"File troppo grande ({file_path.stat().st_size} byte): il limite è 5 MB.")
            data_payload = {
                "documentName": arguments["document_name"],
                "email": {
                    "subject": arguments["email_subject"],
                    "body": arguments["email_body"],
                },
                "signers": arguments["signers"],
                "placeholders": arguments["placeholders"],
            }
            if "signature_type" in arguments and arguments["signature_type"]:
                data_payload["signature_type"] = arguments["signature_type"]
            status, body, _ = _request(
                "POST",
                "/pdfs/documents",
                files={"file": (file_path.name, file_path.read_bytes(), "application/pdf")},
                data={"data": json.dumps(data_payload, ensure_ascii=False)},
                timeout=DOWNLOAD_TIMEOUT,
            )
            if status in (200, 201):
                return _ok(body)
            hint = None
            if status == 422:
                hint = "Crediti FEA insufficienti per il numero di firmatari."
            elif status == 400:
                hint = "Verifica che il PDF non sia ruotato e che signers/placeholders siano coerenti."
            return _err(status, body, hint=hint)

        if name == "preview_pdf_template":
            doc_id = arguments["document_id"]
            return _download_binary_to_disk(
                f"/pdfs/templates/{doc_id}/preview",
                arguments.get("output_path"),
                f"heu_template_{_safe_filename(doc_id)}_preview.pdf",
                "anteprima PDF",
            )

        if name == "update_pdf_template":
            doc_id = arguments["document_id"]
            payload = {"signers": arguments["signers"]}
            if "document_name" in arguments and arguments["document_name"]:
                payload["documentName"] = arguments["document_name"]
            if "placeholders" in arguments and arguments["placeholders"] is not None:
                payload["placeholders"] = arguments["placeholders"]
            status, body, _ = _request("PUT", f"/pdfs/templates/{doc_id}", json=payload)
            if status == 200:
                return _ok(body)
            hint = None
            if status == 400:
                hint = "Il documento potrebbe non essere di tipo 'template', o i dati non sono validi."
            return _err(status, body, hint=hint)

        if name == "delete_pdf_template":
            doc_id = arguments["document_id"]
            status, body, _ = _request("DELETE", f"/pdfs/templates/{doc_id}")
            if status == 204:
                return _ok({"deleted": True, "template_id": doc_id})
            return _err(status, body)

        if name == "cancel_pdf_document":
            doc_id = arguments["document_id"]
            status, body, _ = _request("DELETE", f"/pdfs/documents/{doc_id}")
            if status == 204:
                return _ok({"cancelled": True, "document_id": doc_id, "note": "Richiesta di firma annullata: i firmatari in attesa non possono più firmare."})
            hint = None
            if status == 409:
                hint = "Il documento ha già attività di firma e non può essere annullato."
            return _err(status, body, hint=hint)

        return [TextContent(type="text", text=f"Tool sconosciuto: {name}")]

    except httpx.HTTPError as e:
        return [TextContent(type="text", text=f"Errore HTTP: {str(e)}")]
    except Exception as e:
        return [TextContent(type="text", text=f"Errore: {str(e)}\n{traceback.format_exc()}")]


async def _run():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


def main():
    import asyncio
    asyncio.run(_run())


if __name__ == "__main__":
    main()
