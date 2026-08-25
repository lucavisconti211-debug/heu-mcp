#!/usr/bin/env python3
"""HEU Legal MCP — server remoto (Streamable HTTP) con OAuth 2.1.

Espone gli stessi 28 tool di server.py, ma come servizio multi-utente raggiungibile
via HTTPS. Ogni utente collega il proprio account HEU tramite un flusso OAuth in cui
fornisce la propria API key; il server la conserva cifrata e la usa per le chiamate
effettuate da quell'utente.

Endpoint pubblici:
  GET  /.well-known/oauth-protected-resource   metadati risorsa protetta (RFC 9728)
  GET  /.well-known/oauth-authorization-server metadati authorization server (RFC 8414)
  POST /register                               dynamic client registration (RFC 7591)
  GET  /authorize                              pagina di consenso
  POST /authorize                              conferma consenso -> authorization code
  POST /token                                  scambio code / refresh (form-urlencoded)
  ALL  /mcp                                    endpoint MCP (bearer token richiesto)
  GET  /healthz                                liveness probe

License: MIT
"""

import base64
import contextlib
import hashlib
import html
import json
import os
import secrets
import sqlite3
import time
from urllib.parse import urlencode, urlparse

import anyio
import httpx
from cryptography.fernet import Fernet, InvalidToken
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

import server as heu

# ---------------------------------------------------------------- configurazione

PUBLIC_URL = os.getenv("PUBLIC_URL", "http://localhost:8080").rstrip("/")
MCP_PATH = os.getenv("MCP_PATH", "/mcp")
RESOURCE_URL = f"{PUBLIC_URL}{MCP_PATH}"
DB_PATH = os.getenv("HEU_MCP_DB", "/data/heu-mcp.db")

ACCESS_TOKEN_TTL = int(os.getenv("ACCESS_TOKEN_TTL", str(60 * 60)))          # 1 ora
REFRESH_TOKEN_TTL = int(os.getenv("REFRESH_TOKEN_TTL", str(60 * 60 * 24 * 60)))  # 60 giorni
AUTH_CODE_TTL = 300  # 5 minuti

SCOPES = ["heu:read", "heu:write", "offline_access"]

_secret = os.getenv("HEU_MCP_SECRET_KEY", "")
if not _secret:
    raise SystemExit(
        "HEU_MCP_SECRET_KEY non configurata. Generane una con:\n"
        "  python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
    )
_fernet = Fernet(_secret.encode() if isinstance(_secret, str) else _secret)


# ------------------------------------------------------------------- persistenza

def _db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with _db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS clients (
                client_id TEXT PRIMARY KEY,
                client_name TEXT,
                redirect_uris TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS auth_codes (
                code_hash TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                redirect_uri TEXT NOT NULL,
                code_challenge TEXT NOT NULL,
                scope TEXT NOT NULL,
                heu_key_enc TEXT NOT NULL,
                expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS grants (
                grant_id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                heu_key_enc TEXT NOT NULL,
                scope TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                revoked INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS tokens (
                token_hash TEXT PRIMARY KEY,
                grant_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                used INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_tokens_grant ON tokens(grant_id);
            """
        )


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _encrypt(value: str) -> str:
    return _fernet.encrypt(value.encode()).decode()


def _decrypt(value: str) -> str:
    return _fernet.decrypt(value.encode()).decode()


def _purge_expired():
    now = int(time.time())
    with _db() as c:
        c.execute("DELETE FROM auth_codes WHERE expires_at < ?", (now,))
        c.execute("DELETE FROM tokens WHERE expires_at < ?", (now,))


# ------------------------------------------------------------------- utility OAuth

def _redirect_uri_allowed(registered: list[str], candidate: str) -> bool:
    """Confronto esatto, con eccezione loopback: per RFC 8252 la porta va ignorata."""
    if candidate in registered:
        return True
    cand = urlparse(candidate)
    if cand.hostname not in ("localhost", "127.0.0.1", "::1"):
        return False
    for reg in registered:
        r = urlparse(reg)
        if (
            r.scheme == cand.scheme
            and r.hostname == cand.hostname
            and (r.path or "/") == (cand.path or "/")
        ):
            return True
    return False


def _verify_pkce(verifier: str, challenge: str) -> bool:
    digest = hashlib.sha256(verifier.encode()).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return secrets.compare_digest(expected, challenge)


def _oauth_error(error: str, description: str = "", status: int = 400) -> JSONResponse:
    payload = {"error": error}
    if description:
        payload["error_description"] = description
    return JSONResponse(payload, status_code=status)


async def _validate_heu_key(api_key: str) -> tuple[bool, str]:
    """Verifica che la API key sia valida chiamando un endpoint autenticato di HEU."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{heu.BASE_URL}/documents",
                headers={"x-api-key": api_key, "Accept": "application/json"},
                params={"type": "template"},
            )
    except httpx.HTTPError as e:
        return False, f"Impossibile contattare l'API HEU: {e}"
    if resp.status_code in (200, 201):
        return True, ""
    if resp.status_code in (401, 403):
        return False, "API key non valida o non autorizzata."
    if resp.status_code == 429:
        return False, "Troppe richieste verso l'API HEU. Riprova tra qualche minuto."
    return False, f"L'API HEU ha risposto {resp.status_code}."


# ------------------------------------------------------------------ endpoint OAuth

async def protected_resource_metadata(request: Request):
    return JSONResponse(
        {
            "resource": RESOURCE_URL,
            "authorization_servers": [PUBLIC_URL],
            "scopes_supported": SCOPES,
            "bearer_methods_supported": ["header"],
            "resource_documentation": "https://github.com/heulegal/heu-mcp",
        }
    )


async def authorization_server_metadata(request: Request):
    return JSONResponse(
        {
            "issuer": PUBLIC_URL,
            "authorization_endpoint": f"{PUBLIC_URL}/authorize",
            "token_endpoint": f"{PUBLIC_URL}/token",
            "registration_endpoint": f"{PUBLIC_URL}/register",
            "scopes_supported": SCOPES,
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "client_id_metadata_document_supported": True,
            "service_documentation": "https://github.com/heulegal/heu-mcp",
        }
    )


async def register(request: Request):
    """Dynamic Client Registration (RFC 7591). Client pubblici, nessun secret."""
    try:
        body = await request.json()
    except Exception:
        return _oauth_error("invalid_client_metadata", "Body JSON non valido.")

    redirect_uris = body.get("redirect_uris") or []
    if not isinstance(redirect_uris, list) or not redirect_uris:
        return _oauth_error("invalid_redirect_uri", "redirect_uris è obbligatorio.")
    for uri in redirect_uris:
        parsed = urlparse(uri)
        if parsed.scheme == "https":
            continue
        if parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1"):
            continue
        return _oauth_error("invalid_redirect_uri", f"redirect_uri non ammesso: {uri}")

    client_id = f"heu-{secrets.token_urlsafe(24)}"
    client_name = str(body.get("client_name") or "MCP Client")[:200]
    now = int(time.time())
    with _db() as c:
        c.execute(
            "INSERT INTO clients (client_id, client_name, redirect_uris, created_at) VALUES (?,?,?,?)",
            (client_id, client_name, json.dumps(redirect_uris), now),
        )
    return JSONResponse(
        {
            "client_id": client_id,
            "client_id_issued_at": now,
            "redirect_uris": redirect_uris,
            "client_name": client_name,
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
        status_code=201,
    )


def _lookup_client(client_id: str):
    with _db() as c:
        row = c.execute("SELECT * FROM clients WHERE client_id = ?", (client_id,)).fetchone()
    return row


# Client ID Metadata Document: il client_id è un URL https che serve il proprio
# documento di metadati, invece di essere registrato tramite DCR.
CIMD_TTL = 3600
_cimd_cache: dict[str, tuple[dict, float]] = {}


async def _fetch_cimd(client_id: str) -> dict | None:
    """Scarica e valida un Client ID Metadata Document."""
    cached = _cimd_cache.get(client_id)
    if cached and (time.time() - cached[1]) < CIMD_TTL:
        return cached[0]

    parsed = urlparse(client_id)
    # Solo https, e mai verso host locali/privati (protezione SSRF).
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    host = parsed.hostname.lower()
    if host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local"):
        return None

    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=False) as c:
            resp = await c.get(client_id, headers={"Accept": "application/json"})
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    try:
        doc = resp.json()
    except Exception:
        return None
    # Il documento deve dichiarare come proprio client_id la stessa URL da cui è servito.
    if not isinstance(doc, dict) or doc.get("client_id") != client_id:
        return None
    if not isinstance(doc.get("redirect_uris"), list) or not doc["redirect_uris"]:
        return None

    _cimd_cache[client_id] = (doc, time.time())
    return doc


async def _resolve_client(client_id: str) -> tuple[str, list[str]] | None:
    """Risolve un client sia registrato via DCR sia identificato via CIMD.

    Ritorna (nome visualizzato, redirect_uris ammessi) oppure None."""
    if not client_id:
        return None
    if client_id.startswith("https://"):
        doc = await _fetch_cimd(client_id)
        if not doc:
            return None
        return (str(doc.get("client_name") or "Un'applicazione")[:200], doc["redirect_uris"])
    row = _lookup_client(client_id)
    if not row:
        return None
    return (row["client_name"] or "Un'applicazione", json.loads(row["redirect_uris"]))


CONSENT_PAGE = """<!doctype html>
<html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Collega HEU Legal</title>
<style>
 :root{color-scheme:light dark}
 body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:0;
   display:flex;min-height:100vh;align-items:center;justify-content:center;background:#f5f5f7;color:#1d1d1f}
 @media (prefers-color-scheme:dark){body{background:#1a1a1c;color:#f5f5f7}.card{background:#252528!important}}
 .card{background:#fff;padding:2.5rem;border-radius:16px;max-width:460px;width:calc(100% - 2rem);
   box-shadow:0 4px 24px rgba(0,0,0,.08)}
 h1{font-size:1.35rem;margin:0 0 .5rem}
 p{font-size:.92rem;line-height:1.5;opacity:.8;margin:.5rem 0}
 label{display:block;font-size:.85rem;font-weight:600;margin:1.25rem 0 .4rem}
 input{width:100%;padding:.7rem .8rem;font-size:.95rem;border:1px solid #d2d2d7;border-radius:9px;
   box-sizing:border-box;background:transparent;color:inherit;font-family:ui-monospace,monospace}
 button{width:100%;margin-top:1.25rem;padding:.8rem;font-size:.95rem;font-weight:600;color:#fff;
   background:#0071e3;border:0;border-radius:9px;cursor:pointer}
 button:hover{background:#0077ed}
 .who{font-size:.82rem;background:rgba(127,127,127,.12);padding:.7rem .85rem;border-radius:9px;margin:1rem 0}
 .err{background:#ffebe9;border:1px solid #ff8182;color:#8b1a17;padding:.7rem .85rem;
   border-radius:9px;font-size:.87rem;margin:1rem 0}
 @media (prefers-color-scheme:dark){.err{background:#3d1513;color:#ffb4b0}}
 a{color:#0071e3}
</style></head>
<body><form class="card" method="post" action="/authorize">
<h1>Collega il tuo account HEU Legal</h1>
<p>__CLIENT__ chiede di accedere ai tuoi documenti e firme elettroniche su HEU Legal.</p>
<div class="who">Inserisci la tua <strong>API key HEU</strong>. La trovi nella piattaforma HEU in
<em>Profile → API Keys</em> (richiede il piano Enterprise). Viene conservata cifrata e usata
solo per eseguire le azioni che richiedi.</div>
__ERROR__
<label for="api_key">API key HEU</label>
<input id="api_key" name="api_key" type="password" autocomplete="off" required
  placeholder="sk_live_..." spellcheck="false">
__STATE__
<button type="submit">Collega account</button>
<p style="text-align:center;margin-top:1rem;font-size:.8rem">
<a href="https://www.heulegal.com/privacy-policy/" target="_blank" rel="noopener">Informativa privacy</a></p>
</form></body></html>"""


def _render_consent(params: dict, client_name: str, error: str = "") -> HTMLResponse:
    hidden = "".join(
        f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">'
        for k, v in params.items()
        if v
    )
    page = (
        CONSENT_PAGE.replace("__CLIENT__", html.escape(client_name))
        .replace("__STATE__", hidden)
        .replace("__ERROR__", f'<div class="err">{html.escape(error)}</div>' if error else "")
    )
    return HTMLResponse(page, status_code=400 if error else 200)


async def authorize_get(request: Request):
    q = request.query_params
    client_id = q.get("client_id", "")
    redirect_uri = q.get("redirect_uri", "")
    challenge = q.get("code_challenge", "")

    if q.get("response_type") != "code":
        return _oauth_error("unsupported_response_type", "Solo response_type=code è supportato.")
    if q.get("code_challenge_method") != "S256":
        return _oauth_error("invalid_request", "PKCE con code_challenge_method=S256 è obbligatorio.")
    if not challenge:
        return _oauth_error("invalid_request", "code_challenge mancante.")

    resolved = await _resolve_client(client_id)
    if not resolved:
        return _oauth_error("invalid_client", "client_id sconosciuto o documento CIMD non valido.")
    client_name, allowed_uris = resolved
    if not _redirect_uri_allowed(allowed_uris, redirect_uri):
        return _oauth_error("invalid_request", "redirect_uri non registrato per questo client.")

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": challenge,
        "state": q.get("state", ""),
        "scope": q.get("scope", "heu:read heu:write"),
    }
    return _render_consent(params, client_name)


async def authorize_post(request: Request):
    form = await request.form()
    client_id = str(form.get("client_id", ""))
    redirect_uri = str(form.get("redirect_uri", ""))
    challenge = str(form.get("code_challenge", ""))
    state = str(form.get("state", ""))
    scope = str(form.get("scope", "heu:read heu:write"))
    api_key = str(form.get("api_key", "")).strip()

    resolved = await _resolve_client(client_id)
    if not resolved or not challenge:
        return _oauth_error("invalid_client", "Sessione di autorizzazione non valida.")
    client_name, allowed_uris = resolved
    if not _redirect_uri_allowed(allowed_uris, redirect_uri):
        return _oauth_error("invalid_request", "redirect_uri non registrato per questo client.")

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": challenge,
        "state": state,
        "scope": scope,
    }
    if not api_key:
        return _render_consent(params, client_name, "Inserisci la tua API key HEU.")

    ok, message = await _validate_heu_key(api_key)
    if not ok:
        return _render_consent(params, client_name, message)

    code = secrets.token_urlsafe(32)
    with _db() as c:
        c.execute(
            "INSERT INTO auth_codes (code_hash, client_id, redirect_uri, code_challenge, scope,"
            " heu_key_enc, expires_at) VALUES (?,?,?,?,?,?,?)",
            (_hash(code), client_id, redirect_uri, challenge, scope,
             _encrypt(api_key), int(time.time()) + AUTH_CODE_TTL),
        )
    qs = {"code": code}
    if state:
        qs["state"] = state
    sep = "&" if urlparse(redirect_uri).query else "?"
    return RedirectResponse(f"{redirect_uri}{sep}{urlencode(qs)}", status_code=302)


def _issue_tokens(grant_id: str, scope: str) -> dict:
    access = secrets.token_urlsafe(40)
    refresh = secrets.token_urlsafe(40)
    now = int(time.time())
    with _db() as c:
        c.execute(
            "INSERT INTO tokens (token_hash, grant_id, kind, expires_at) VALUES (?,?,?,?)",
            (_hash(access), grant_id, "access", now + ACCESS_TOKEN_TTL),
        )
        c.execute(
            "INSERT INTO tokens (token_hash, grant_id, kind, expires_at) VALUES (?,?,?,?)",
            (_hash(refresh), grant_id, "refresh", now + REFRESH_TOKEN_TTL),
        )
    return {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": ACCESS_TOKEN_TTL,
        "refresh_token": refresh,
        "scope": scope,
    }


async def token(request: Request):
    _purge_expired()
    try:
        form = await request.form()
    except Exception:
        return _oauth_error("invalid_request", "Atteso Content-Type application/x-www-form-urlencoded.")

    grant_type = str(form.get("grant_type", ""))
    client_id = str(form.get("client_id", ""))

    if grant_type == "authorization_code":
        code = str(form.get("code", ""))
        verifier = str(form.get("code_verifier", ""))
        redirect_uri = str(form.get("redirect_uri", ""))
        if not (code and verifier):
            return _oauth_error("invalid_request", "code e code_verifier sono obbligatori.")

        with _db() as c:
            row = c.execute("SELECT * FROM auth_codes WHERE code_hash = ?", (_hash(code),)).fetchone()
            # Un authorization code è monouso: eliminalo appena letto.
            c.execute("DELETE FROM auth_codes WHERE code_hash = ?", (_hash(code),))

        if not row:
            return _oauth_error("invalid_grant", "Authorization code non valido o già usato.")
        if row["expires_at"] < int(time.time()):
            return _oauth_error("invalid_grant", "Authorization code scaduto.")
        if row["client_id"] != client_id:
            return _oauth_error("invalid_grant", "Il code non appartiene a questo client.")
        if redirect_uri and redirect_uri != row["redirect_uri"]:
            return _oauth_error("invalid_grant", "redirect_uri non corrispondente.")
        if not _verify_pkce(verifier, row["code_challenge"]):
            return _oauth_error("invalid_grant", "Verifica PKCE fallita.")

        grant_id = secrets.token_urlsafe(24)
        with _db() as c:
            c.execute(
                "INSERT INTO grants (grant_id, client_id, heu_key_enc, scope, created_at) VALUES (?,?,?,?,?)",
                (grant_id, client_id, row["heu_key_enc"], row["scope"], int(time.time())),
            )
        return JSONResponse(_issue_tokens(grant_id, row["scope"]))

    if grant_type == "refresh_token":
        presented = str(form.get("refresh_token", ""))
        if not presented:
            return _oauth_error("invalid_request", "refresh_token mancante.")
        h = _hash(presented)
        with _db() as c:
            row = c.execute(
                "SELECT t.*, g.scope AS grant_scope, g.revoked AS revoked FROM tokens t"
                " JOIN grants g ON g.grant_id = t.grant_id"
                " WHERE t.token_hash = ? AND t.kind = 'refresh'",
                (h,),
            ).fetchone()
            if not row:
                return _oauth_error("invalid_grant", "Refresh token non valido.")
            if row["revoked"]:
                return _oauth_error("invalid_grant", "Autorizzazione revocata.")
            if row["expires_at"] < int(time.time()):
                return _oauth_error("invalid_grant", "Refresh token scaduto.")
            if row["used"]:
                # Riuso di un refresh token già ruotato: possibile furto, revoca tutto.
                c.execute("UPDATE grants SET revoked = 1 WHERE grant_id = ?", (row["grant_id"],))
                c.execute("DELETE FROM tokens WHERE grant_id = ?", (row["grant_id"],))
                return _oauth_error("invalid_grant", "Refresh token già utilizzato; sessione revocata.")
            # Rotazione: invalida il vecchio nella stessa risposta che ne emette uno nuovo.
            c.execute("UPDATE tokens SET used = 1 WHERE token_hash = ?", (h,))
            c.execute(
                "DELETE FROM tokens WHERE grant_id = ? AND kind = 'access'", (row["grant_id"],)
            )
        return JSONResponse(_issue_tokens(row["grant_id"], row["grant_scope"]))

    return _oauth_error("unsupported_grant_type", f"grant_type non supportato: {grant_type}")


# --------------------------------------------------------------------- endpoint MCP

def _unauthorized(description: str = "Autenticazione richiesta") -> Response:
    return JSONResponse(
        {"error": "invalid_token", "error_description": description},
        status_code=401,
        headers={
            "WWW-Authenticate": (
                f'Bearer resource_metadata="{PUBLIC_URL}/.well-known/oauth-protected-resource", '
                f'scope="heu:read heu:write"'
            )
        },
    )


def _api_key_for_token(bearer: str) -> str | None:
    with _db() as c:
        row = c.execute(
            "SELECT g.heu_key_enc AS enc, g.revoked AS revoked, t.expires_at AS exp"
            " FROM tokens t JOIN grants g ON g.grant_id = t.grant_id"
            " WHERE t.token_hash = ? AND t.kind = 'access'",
            (_hash(bearer),),
        ).fetchone()
    if not row or row["revoked"] or row["exp"] < int(time.time()):
        return None
    try:
        return _decrypt(row["enc"])
    except InvalidToken:
        return None


session_manager = StreamableHTTPSessionManager(app=heu.app, json_response=False, stateless=True)


class McpEndpoint:
    """App ASGI per l'endpoint MCP.

    È una classe (e non una funzione) perché Starlette tratta le funzioni come
    handler request/response: solo un oggetto chiamabile viene passato come app
    ASGI, necessaria qui per lo streaming SSE.
    """

    async def __call__(self, scope, receive, send):
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        auth = headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            await _unauthorized()(scope, receive, send)
            return
        api_key = await anyio.to_thread.run_sync(_api_key_for_token, auth[7:].strip())
        if not api_key:
            await _unauthorized("Token non valido o scaduto")(scope, receive, send)
            return
        tok = heu.set_api_key(api_key)
        try:
            await session_manager.handle_request(scope, receive, send)
        finally:
            heu._api_key_var.reset(tok)


mcp_endpoint = McpEndpoint()


async def healthz(request: Request):
    return JSONResponse({"status": "ok", "tools": 28, "resource": RESOURCE_URL})


async def index(request: Request):
    return HTMLResponse(
        "<!doctype html><meta charset='utf-8'><title>HEU Legal MCP</title>"
        "<div style=\"font-family:-apple-system,sans-serif;max-width:640px;margin:4rem auto;padding:0 1rem\">"
        "<h1>HEU Legal MCP Server</h1>"
        f"<p>Endpoint MCP: <code>{html.escape(RESOURCE_URL)}</code></p>"
        "<p>Aggiungilo come connettore nel tuo client MCP; ti verrà chiesta la tua API key HEU.</p>"
        "<p><a href='https://github.com/heulegal/heu-mcp'>Documentazione</a> · "
        "<a href='https://www.heulegal.com/privacy-policy/'>Privacy</a></p></div>"
    )


@contextlib.asynccontextmanager
async def lifespan(app):
    init_db()
    async with session_manager.run():
        yield


app = Starlette(
    lifespan=lifespan,
    routes=[
        Route("/", index),
        Route("/healthz", healthz),
        Route("/.well-known/oauth-protected-resource", protected_resource_metadata),
        Route(f"/.well-known/oauth-protected-resource{MCP_PATH}", protected_resource_metadata),
        Route("/.well-known/oauth-authorization-server", authorization_server_metadata),
        Route("/register", register, methods=["POST"]),
        Route("/authorize", authorize_get, methods=["GET"]),
        Route("/authorize", authorize_post, methods=["POST"]),
        Route("/token", token, methods=["POST"]),
        # Il path esatto va servito da una Route: un Mount da solo risponderebbe
        # 307 verso "/mcp/", mentre i client MCP interrogano l'URL senza slash.
        Route(MCP_PATH, mcp_endpoint, methods=["GET", "POST", "DELETE", "OPTIONS"]),
        Mount(MCP_PATH, app=mcp_endpoint),
    ],
)


def main():
    import uvicorn

    uvicorn.run(
        "remote_server:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8080")),
        log_level=os.getenv("LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
