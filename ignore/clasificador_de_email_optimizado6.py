"""
Clasificador de email con triaje enriquecido — v6 OPTIMIZADO
- Análisis técnico de headers/links ANTES del LLM
- Esquema JSON con múltiples dimensiones (categoría, importancia, urgencia, etc.)
- Caché SQLite por hash de email
- Tabla resumen y lista de acciones priorizadas
- ⚡ Optimizaciones de rendimiento:
    * keep_alive para no descargar el modelo entre llamadas
    * Precarga única del modelo antes del bucle
    * num_predict limitado + decodificación greedy
    * Prompt compacto y contexto reducido
    * Pre-filtro heurístico para newsletters conocidas
    * Fallback desactivado por defecto (opt-in vía USE_FALLBACK=true)
"""

import os
import re
import json
import time
import sqlite3
import hashlib
import imaplib
import email
import urllib.request
import urllib.error
from email.header import decode_header
from datetime import datetime, timedelta
from urllib.parse import urlparse
from pathlib import Path

from dotenv import load_dotenv

# ══════════════════════════════════════════════════════════════════════
# CONFIGURACIÓN
# ══════════════════════════════════════════════════════════════════════

# Carga .env robusta (funciona aunque ejecutes desde otro directorio)
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

GMAIL_USER = (os.getenv("GMAIL_USER") or "").strip()
GMAIL_APP_PASSWORD = (os.getenv("GMAIL_APP_PASSWORD") or "").strip().strip('"').strip("'")

HOURS_TO_FETCH = 48

# ⚡ Optimizado para Intel i5 (4 núcleos) + 2GB VRAM + 12GB RAM
CPU_THREADS = 4
CONTEXT_SIZE = 512                          # ⚡ antes 1024, con prompts ~400 sobra
OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:0.5b")   # ⚡ 0.5b por defecto, 5-10x más rápido
OLLAMA_FALLBACK_MODEL = os.getenv("OLLAMA_FALLBACK_MODEL", "qwen2.5:1.5b")
USE_FALLBACK = os.getenv("USE_FALLBACK", "false").lower() == "true"   # ⚡ off por defecto
KEEP_ALIVE = "2h"                           # ⚡ mantiene el modelo en VRAM
NUM_PREDICT = 220                           # ⚡ límite duro de tokens de salida
OLLAMA_TIMEOUT = 120                        # segundos

DB_PATH = BASE_DIR / "cache_emails.db"

ENGLISH_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# ⚡ Lista blanca de dominios que son newsletters/promos conocidas.
# Se saltan el LLM por completo → clasificación instantánea.
REMITENTES_NEWSLETTER = {
    "substack.com", "beehiiv.com", "producthunt.com", "linkedin.com",
    "github.com", "codepen.io", "buffer.com", "supabase.com", "make.com",
    "inoreader.com", "malt.com", "certificates.dev", "cursor.com",
    "reedsy.com", "ollama.com", "medium.com", "dev.to", "hashnode.com",
}

# ══════════════════════════════════════════════════════════════════════
# ESQUEMA DE CLASIFICACIÓN Y PROMPT
# ══════════════════════════════════════════════════════════════════════

CATEGORIAS = [
    "Newsletter", "Notificacion", "Transaccional", "Trabajo",
    "Personal", "Promocion", "Spam", "Phishing"
]

CLASSIFICATION_SCHEMA = {
    "type": "object",
    "properties": {
        "resumen":             {"type": "string", "maxLength": 120},
        "categoria":           {"type": "string", "enum": CATEGORIAS},
        "importancia":         {"type": "integer", "minimum": 1, "maximum": 5},
        "urgencia":            {"type": "string", "enum": ["baja", "media", "alta"]},
        "requiere_respuesta":  {"type": "boolean"},
        "requiere_accion":     {"type": "boolean"},
        "accion_sugerida":     {"type": "string"},
        "plazo":               {"type": "string"},
        "riesgo_seguridad":    {"type": "string", "enum": ["ninguno", "bajo", "medio", "alto"]},
        "confianza":           {"type": "integer", "minimum": 0, "maximum": 100},
        "razon_breve":         {"type": "string", "maxLength": 160}
    },
    "required": [
        "resumen", "categoria", "importancia", "urgencia",
        "requiere_respuesta", "requiere_accion", "accion_sugerida",
        "plazo", "riesgo_seguridad", "confianza", "razon_breve"
    ]
}

# ⚡ Prompt compacto (antes ~400 tokens, ahora ~150)
SYSTEM_PROMPT = """Triaje de email. Devuelve SOLO JSON válido.

Reglas:
- "Phishing" SOLO con evidencia clara: dominio falso, urgencia pidiendo credenciales, links que no coinciden con el remitente.
- Marcas conocidas (LinkedIn, Substack, GitHub, Supabase, Google, Product Hunt, Beehiiv, etc.) = "Newsletter"/"Promocion"/"Notificacion", NUNCA Phishing.
- Alertas de dominios oficiales (accounts.google.com) = "Notificacion", riesgo bajo.
- Trabajo/reclutadores = "Trabajo". Recibos/confirmaciones = "Transaccional". Personas = "Personal".
- En duda, elige categoría legítima y riesgo "bajo".
- Importancia: 1=ruido, 3=normal, 5=crítico (seguridad/dinero/trabajo urgente).
- No inventes datos. Basa "razon_breve" en el contenido visible."""

# ══════════════════════════════════════════════════════════════════════
# CACHÉ SQLITE
# ══════════════════════════════════════════════════════════════════════

def init_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS clasificaciones (
            hash        TEXT PRIMARY KEY,
            message_id  TEXT,
            sender      TEXT,
            subject     TEXT,
            fecha       TEXT,
            resultado   TEXT
        )
    """)
    conn.commit()
    return conn


def hash_email(texto):
    return hashlib.sha256(texto.encode("utf-8", errors="ignore")).hexdigest()


def cache_get(conn, h):
    row = conn.execute(
        "SELECT resultado FROM clasificaciones WHERE hash = ?", (h,)
    ).fetchone()
    return json.loads(row[0]) if row else None


def cache_set(conn, h, msg_id, sender, subject, resultado):
    conn.execute(
        "INSERT OR REPLACE INTO clasificaciones "
        "(hash, message_id, sender, subject, fecha, resultado) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (h, msg_id, sender, subject,
         datetime.now().isoformat(), json.dumps(resultado, ensure_ascii=False))
    )
    conn.commit()

# ══════════════════════════════════════════════════════════════════════
# DESCARGA DE EMAILS
# ══════════════════════════════════════════════════════════════════════

def decodificar_header(valor):
    if not valor:
        return ""
    partes = decode_header(valor)
    out = []
    for contenido, enc in partes:
        if isinstance(contenido, bytes):
            try:
                out.append(contenido.decode(enc or "utf-8", errors="ignore"))
            except (LookupError, UnicodeDecodeError):
                out.append(contenido.decode("utf-8", errors="ignore"))
        else:
            out.append(contenido)
    return "".join(out)


def extraer_cuerpo(msg, max_chars=1200):
    """Prefiere text/plain; si no, limpia un poco el HTML."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    return part.get_payload(decode=True).decode("utf-8", errors="ignore")[:max_chars]
                except Exception:
                    pass
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                try:
                    html = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                    texto = re.sub(r"<[^>]+>", " ", html)
                    texto = re.sub(r"\s+", " ", texto)
                    return texto[:max_chars]
                except Exception:
                    pass
        return ""
    else:
        try:
            return msg.get_payload(decode=True).decode("utf-8", errors="ignore")[:max_chars]
        except Exception:
            return ""


def analizar_señales(msg):
    """Señales técnicas objetivas para dar contexto al LLM."""
    señales = {}

    auth = msg.get("Authentication-Results", "")
    for k in ("spf", "dkim", "dmarc"):
        if f"{k}=pass" in auth:
            señales[k] = "pass"
        elif f"{k}=fail" in auth:
            señales[k] = "fail"
        else:
            señales[k] = "unknown"

    sender = msg.get("From", "")
    m = re.search(r"@([\w\.\-]+)", sender)
    dominio_from = m.group(1).lower() if m else "desconocido"
    señales["dominio_remitente"] = dominio_from

    reply_to = msg.get("Reply-To", "")
    señales["reply_to_distinto"] = bool(reply_to) and reply_to.strip() != sender.strip()

    dominios_enlaces = set()
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            try:
                html = part.get_payload(decode=True).decode("utf-8", errors="ignore")
            except Exception:
                continue
            for url in re.findall(r'href="(https?://[^"]+)"', html):
                try:
                    dominios_enlaces.add(urlparse(url).netloc.lower())
                except Exception:
                    pass
            break

    señales["dominios_enlaces"] = sorted(dominios_enlaces)[:8]

    if dominios_enlaces and dominio_from != "desconocido":
        raiz = ".".join(dominio_from.split(".")[-2:])
        señales["mismatch_dominio"] = not any(raiz in d for d in dominios_enlaces)
    else:
        señales["mismatch_dominio"] = False

    return señales


def fetch_gmail_emails(username, app_password, hours=48):
    print(f"\n--- Conectando a Gmail IMAP ({username}) ---")
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(username, app_password.replace(" ", "").strip())
        mail.select("inbox")

        target_date = datetime.now() - timedelta(hours=hours)
        since = f"{target_date.day:02d}-{ENGLISH_MONTHS[target_date.month - 1]}-{target_date.year}"
        print(f"Buscando correos desde: {since}")

        status, messages = mail.search(None, f'(SINCE "{since}")')
        if status != "OK" or not messages[0]:
            print("Sin correos en ese rango.")
            mail.logout()
            return []

        ids = messages[0].split()
        print(f"Encontrados {len(ids)} correos.")

        resultados = []
        for e_id in ids:
            _, data = mail.fetch(e_id, "(RFC822)")
            for part in data:
                if not isinstance(part, tuple):
                    continue
                msg = email.message_from_bytes(part[1])

                subject = decodificar_header(msg.get("Subject", "(Sin Asunto)"))
                sender = decodificar_header(msg.get("From", "Desconocido"))
                message_id = msg.get("Message-ID", e_id.decode())

                cuerpo = extraer_cuerpo(msg, max_chars=1200)
                señales = analizar_señales(msg)

                texto_llm = (
                    f"Subject: {subject}\n"
                    f"From: {sender}\n"
                    f"Content: {cuerpo.strip()}"
                )

                resultados.append({
                    "id": e_id.decode(),
                    "message_id": message_id,
                    "subject": subject,
                    "sender": sender,
                    "texto_llm": texto_llm,
                    "señales": señales,
                })

        mail.logout()
        return resultados

    except imaplib.IMAP4.error as e:
        print(f"[ERROR IMAP] {e}")
        return []
    except Exception as e:
        print(f"[ERROR GENERAL] {e}")
        return []

# ══════════════════════════════════════════════════════════════════════
# ⚡ PRE-FILTRO HEURÍSTICO (evita llamar al LLM en newsletters obvias)
# ══════════════════════════════════════════════════════════════════════

def pre_clasificar(sender):
    """Si el remitente es una newsletter conocida, devuelve una clasificación por defecto."""
    m = re.search(r"@([\w\.\-]+)", sender)
    if not m:
        return None
    dominio = m.group(1).lower()
    raiz = ".".join(dominio.split(".")[-2:])
    if raiz in REMITENTES_NEWSLETTER:
        return {
            "resumen": "Newsletter de remitente conocido",
            "categoria": "Newsletter",
            "importancia": 1,
            "urgencia": "baja",
            "requiere_respuesta": False,
            "requiere_accion": False,
            "accion_sugerida": "Ninguna",
            "plazo": "ninguno",
            "riesgo_seguridad": "ninguno",
            "confianza": 95,
            "razon_breve": "Remitente en lista blanca de newsletters.",
        }
    return None

# ══════════════════════════════════════════════════════════════════════
# CLASIFICACIÓN CON OLLAMA
# ══════════════════════════════════════════════════════════════════════

def _ollama_call(modelo, prompt, schema):
    payload = {
        "model": modelo,
        "prompt": prompt,
        "stream": False,
        "format": schema,
        "keep_alive": KEEP_ALIVE,        # ⚡ mantiene el modelo en VRAM 2h
        "options": {
            "num_gpu": 99,
            "num_ctx": CONTEXT_SIZE,
            "num_thread": CPU_THREADS,
            "temperature": 0.0,
            "num_predict": NUM_PREDICT,  # ⚡ límite duro de salida
            "top_k": 1,                  # ⚡ greedy: 1 sola opción por token
            "top_p": 0.0,
            "repeat_penalty": 1.0,
        }
    }
    req = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as r:
        data = json.loads(r.read().decode("utf-8"))
        return json.loads(data.get("response", "{}"))


def preload_model(modelo):
    """⚡ Fuerza a Ollama a cargar el modelo en VRAM antes del bucle principal."""
    print(f"[Precarga] Cargando {modelo} en memoria...", end=" ", flush=True)
    t0 = time.time()
    payload = {
        "model": modelo,
        "prompt": "hi",
        "stream": False,
        "keep_alive": KEEP_ALIVE,
        "options": {"num_predict": 1, "num_ctx": 128, "num_gpu": 99},
    }
    req = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            r.read()
        print(f"OK ({time.time() - t0:.1f}s)")
    except Exception as e:
        print(f"FALLÓ: {e}")


def classify_with_ollama(texto_llm, señales):
    señales_txt = "\n".join(f"- {k}: {v}" for k, v in señales.items())

    prompt = f"""{SYSTEM_PROMPT}

SEÑALES TÉCNICAS:
{señales_txt}

EMAIL:
{texto_llm}

Devuelve SOLO el JSON."""

    try:
        res = _ollama_call(OLLAMA_MODEL, prompt, CLASSIFICATION_SCHEMA)

        # ⚡ Fallback desactivado por defecto (USE_FALLBACK=true para activarlo)
        if USE_FALLBACK and res and res.get("confianza", 100) < 60:
            print("   ↪ baja confianza, usando modelo mayor (lento)...")
            try:
                res = _ollama_call(OLLAMA_FALLBACK_MODEL, prompt, CLASSIFICATION_SCHEMA)
            except Exception:
                pass

        return res

    except urllib.error.URLError as e:
        print(f"[ERROR OLLAMA] {e}")
        return None
    except Exception as e:
        print(f"[ERROR CLASIFICANDO] {e}")
        return None

# ══════════════════════════════════════════════════════════════════════
# PRESENTACIÓN DE RESULTADOS
# ══════════════════════════════════════════════════════════════════════

ORDEN_URGENCIA = {"alta": 3, "media": 2, "baja": 1}
ORDEN_RIESGO   = {"alto": 4, "medio": 3, "bajo": 2, "ninguno": 1}


def imprimir_tabla(resultados):
    resultados = sorted(
        resultados,
        key=lambda r: (
            r["clasificacion"].get("importancia", 0),
            ORDEN_URGENCIA.get(r["clasificacion"].get("urgencia", "baja"), 0),
            ORDEN_RIESGO.get(r["clasificacion"].get("riesgo_seguridad", "ninguno"), 0),
        ),
        reverse=True,
    )

    print("\n" + "═" * 130)
    print(f"{'IMP':<4}{'URG':<7}{'CATEGORIA':<15}{'RESP':<6}{'ACC':<5}{'RIESGO':<9}{'CONF':<6}ASUNTO")
    print("─" * 130)

    for r in resultados:
        c = r["clasificacion"]
        asunto = r["subject"][:60] + ("…" if len(r["subject"]) > 60 else "")
        print(
            f"{c.get('importancia', '?'):<4}"
            f"{c.get('urgencia', '?'):<7}"
            f"{c.get('categoria', '?'):<15}"
            f"{'Sí' if c.get('requiere_respuesta') else '—':<6}"
            f"{'Sí' if c.get('requiere_accion') else '—':<5}"
            f"{c.get('riesgo_seguridad', '?'):<9}"
            f"{c.get('confianza', 0):<6}"
            f"{asunto}"
        )

    return resultados


def imprimir_acciones(resultados):
    print("\n" + "═" * 90)
    print(" ACCIONES PRIORITARIAS")
    print("═" * 90)

    hay = False
    for r in resultados:
        c = r["clasificacion"]
        if c.get("requiere_accion") or c.get("riesgo_seguridad") in ("alto", "medio"):
            hay = True
            print(f"\n▸ [{c.get('categoria')}] {r['subject'][:70]}")
            print(f"  De:      {r['sender'][:80]}")
            print(f"  Resumen: {c.get('resumen', '—')}")
            print(f"  Acción:  {c.get('accion_sugerida', '—')}")
            if c.get("plazo") and c["plazo"] != "ninguno":
                print(f"  Plazo:   {c['plazo']}")
            print(f"  Motivo:  {c.get('razon_breve', '—')}")

    if not hay:
        print("\nNo hay acciones pendientes. 🎉")


def imprimir_resumen_ejecutivo(resultados):
    total = len(resultados)
    por_cat = {}
    riesgos = 0
    respuestas = 0
    acciones = 0

    for r in resultados:
        c = r["clasificacion"]
        por_cat[c.get("categoria", "?")] = por_cat.get(c.get("categoria", "?"), 0) + 1
        if c.get("riesgo_seguridad") in ("alto", "medio"):
            riesgos += 1
        if c.get("requiere_respuesta"):
            respuestas += 1
        if c.get("requiere_accion"):
            acciones += 1

    print("\n" + "═" * 90)
    print(" RESUMEN EJECUTIVO")
    print("═" * 90)
    print(f"Total analizados:       {total}")
    print(f"Con riesgo medio/alto:  {riesgos}")
    print(f"Requieren respuesta:    {respuestas}")
    print(f"Requieren acción:       {acciones}")
    print("\nPor categoría:")
    for cat, n in sorted(por_cat.items(), key=lambda x: -x[1]):
        print(f"  {cat:<16} {n}")

# ══════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════

def main():
    t_inicio = time.time()

    print("═" * 60)
    print(" Clasificador de Email — Triaje enriquecido v6")
    print(f" Modelo: {OLLAMA_MODEL} | Fallback: {'ON' if USE_FALLBACK else 'OFF'}")
    print("═" * 60)

    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        print("\n[!] Faltan GMAIL_USER o GMAIL_APP_PASSWORD en el .env")
        return

    # ⚡ Precarga el modelo en VRAM antes del bucle
    preload_model(OLLAMA_MODEL)

    conn = init_db()
    emails = fetch_gmail_emails(GMAIL_USER, GMAIL_APP_PASSWORD, HOURS_TO_FETCH)

    if not emails:
        print("No hay correos que procesar.")
        conn.close()
        return

    resultados = []
    n_cache = n_heuristic = n_llm = 0

    for idx, item in enumerate(emails, 1):
        print(f"\n[{idx}/{len(emails)}] {item['subject'][:70]}")
        h = hash_email(item["texto_llm"])
        cached = cache_get(conn, h)

        if cached:
            print("   ✓ desde caché")
            clasificacion = cached
            n_cache += 1
        else:
            # ⚡ 1. Pre-filtro heurístico (instantáneo)
            clasificacion = pre_clasificar(item["sender"])

            if clasificacion:
                print("   ⚡ pre-filtro newsletter")
                n_heuristic += 1
            else:
                # ⚡ 2. LLM solo para los que no son obvios
                t0 = time.time()
                clasificacion = classify_with_ollama(item["texto_llm"], item["señales"])
                dt = time.time() - t0
                print(f"   🧠 LLM ({dt:.1f}s)")
                n_llm += 1

            if not clasificacion:
                continue
            cache_set(conn, h, item["message_id"], item["sender"],
                      item["subject"], clasificacion)

        print(f"   → [{clasificacion.get('categoria')}] "
              f"imp={clasificacion.get('importancia')} "
              f"urg={clasificacion.get('urgencia')} "
              f"riesgo={clasificacion.get('riesgo_seguridad')} "
              f"conf={clasificacion.get('confianza')}")

        resultados.append({**item, "clasificacion": clasificacion})

    conn.close()

    if resultados:
        ordenados = imprimir_tabla(resultados)
        imprimir_resumen_ejecutivo(ordenados)
        imprimir_acciones(ordenados)

    # ⚡ Estadísticas de tiempo
    total = time.time() - t_inicio
    print("\n" + "═" * 60)
    print(f" Tiempo total:  {total:.1f}s")
    print(f" Desde caché:   {n_cache}")
    print(f" Pre-filtro:    {n_heuristic}")
    print(f" LLM:           {n_llm}")
    if n_llm > 0:
        print(f" Media por LLM: {total / max(n_llm, 1):.1f}s")
    print("═" * 60)


if __name__ == "__main__":
    main()