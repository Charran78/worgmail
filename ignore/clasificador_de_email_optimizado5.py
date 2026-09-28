"""
Clasificador de email con triaje enriquecido.
- Análisis técnico de headers/links ANTES del LLM
- Esquema JSON con múltiples dimensiones (categoría, importancia, urgencia, etc.)
- Caché SQLite por hash de email
- Tabla resumen y lista de acciones priorizadas
"""

import os
import re
import json
import sqlite3
import hashlib
import imaplib
import email
import urllib.request
import urllib.error
from email.header import decode_header
from datetime import datetime, timedelta
from urllib.parse import urlparse

from dotenv import load_dotenv

# ══════════════════════════════════════════════════════════════════════
# CONFIGURACIÓN
# ══════════════════════════════════════════════════════════════════════

load_dotenv()

GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")

HOURS_TO_FETCH = 48

# Optimizado para: Intel i5 (4 núcleos) + 2GB VRAM + 12GB RAM
CPU_THREADS = 4
CONTEXT_SIZE = 1024
OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "qwen2.5:1.5b"          # 0.5b para pruebas, 1.5b mejor precisión
OLLAMA_FALLBACK_MODEL = "qwen2.5:3b"   # para casos de baja confianza (si cabe en VRAM)

DB_PATH = "cache_emails.db"

ENGLISH_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

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
        "resumen": {
            "type": "string",
            "description": "Una línea (max 15 palabras) que resuma el propósito del email."
        },
        "categoria": {
            "type": "string",
            "enum": CATEGORIAS
        },
        "importancia": {
            "type": "integer",
            "minimum": 1,
            "maximum": 5,
            "description": "1=ruido, 3=normal, 5=crítico."
        },
        "urgencia": {
            "type": "string",
            "enum": ["baja", "media", "alta"]
        },
        "requiere_respuesta": {"type": "boolean"},
        "requiere_accion":    {"type": "boolean"},
        "accion_sugerida": {
            "type": "string",
            "description": "Acción concreta en imperativo, o 'Ninguna'."
        },
        "plazo": {
            "type": "string",
            "description": "Fecha límite (YYYY-MM-DD) o 'ninguno'."
        },
        "riesgo_seguridad": {
            "type": "string",
            "enum": ["ninguno", "bajo", "medio", "alto"]
        },
        "confianza": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100
        },
        "razon_breve": {
            "type": "string",
            "description": "Máximo 20 palabras justificando categoría y riesgo."
        }
    },
    "required": [
        "resumen", "categoria", "importancia", "urgencia",
        "requiere_respuesta", "requiere_accion", "accion_sugerida",
        "plazo", "riesgo_seguridad", "confianza", "razon_breve"
    ]
}

SYSTEM_PROMPT = """Eres un asistente experto en triaje de correo electrónico.
Analizas emails y devuelves un JSON con metadatos útiles para que el usuario decida qué hacer con cada uno.

REGLAS ESTRICTAS (respétalas siempre):
1. Un email SOLO es "Phishing" si hay evidencia CLARA:
   - Dominio del remitente sospechoso (typosquatting, dominios raros, TLD baratos)
   - Urgencia artificial pidiendo credenciales, contraseñas o datos bancarios
   - Enlaces cuyo dominio NO coincide con el remitente
   - Adjuntos ejecutables o formularios embebidos sospechosos
2. Newsletters de marcas conocidas (LinkedIn, Substack, GitHub, Supabase, Product Hunt, Beehiiv, Buffer, CodePen, etc.) son "Newsletter" o "Promocion". NUNCA "Phishing".
3. Alertas de seguridad de dominios oficiales (accounts.google.com, etc.) son "Notificacion" con riesgo "bajo", salvo que el enlace NO apunte al dominio oficial.
4. Ofertas de trabajo, mensajes de reclutadores y comunicaciones profesionales -> "Trabajo".
5. Confirmaciones, recibos, envíos -> "Transaccional".
6. Emails de personas que el usuario probablemente conoce -> "Personal".
7. Si dudas entre legítimo y phishing, elige la categoría legítima y riesgo "bajo". NO sobreclasifiques como Phishing.
8. El campo "razon_breve" debe basarse SOLO en datos visibles. Nunca inventes contenido.
9. "importancia": 1 = publicidad/ruido, 3 = normal, 5 = crítico (seguridad, dinero, trabajo urgente).
10. Si el email pide una acción con fecha límite, rellena "plazo" con la fecha en formato YYYY-MM-DD.
"""

# ══════════════════════════════════════════════════════════════════════
# CACHÉ SQLITE
# ══════════════════════════════════════════════════════════════════════

def init_db():
    conn = sqlite3.connect(DB_PATH)
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


def extraer_cuerpo(msg, max_chars=1500):
    """Prefiere text/plain; si no, limpia un poco el HTML."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    return part.get_payload(decode=True).decode("utf-8", errors="ignore")[:max_chars]
                except Exception:
                    pass
        # fallback: HTML
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

    # Autenticación
    auth = msg.get("Authentication-Results", "")
    for k in ("spf", "dkim", "dmarc"):
        if f"{k}=pass" in auth:
            señales[k] = "pass"
        elif f"{k}=fail" in auth:
            señales[k] = "fail"
        else:
            señales[k] = "unknown"

    # Dominio del remitente
    sender = msg.get("From", "")
    m = re.search(r"@([\w\.\-]+)", sender)
    dominio_from = m.group(1).lower() if m else "desconocido"
    señales["dominio_remitente"] = dominio_from

    # Reply-To distinto
    reply_to = msg.get("Reply-To", "")
    señales["reply_to_distinto"] = bool(reply_to) and reply_to.strip() != sender.strip()

    # Links en HTML
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

    # ¿Coincide el dominio raíz del From con algún link?
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

                cuerpo = extraer_cuerpo(msg, max_chars=1500)
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
# CLASIFICACIÓN CON OLLAMA
# ══════════════════════════════════════════════════════════════════════

def _ollama_call(modelo, prompt, schema):
    payload = {
        "model": modelo,
        "prompt": prompt,
        "stream": False,
        "format": schema,
	"keep_alive": "2h",
        "options": {
            "num_gpu": 99,
            "num_ctx": CONTEXT_SIZE,
            "num_thread": CPU_THREADS,
            "temperature": 0.0,
        }
    }
    req = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=90) as r:
        data = json.loads(r.read().decode("utf-8"))
        return json.loads(data.get("response", "{}"))


def classify_with_ollama(texto_llm, señales):
    señales_txt = "\n".join(
        f"- {k}: {v}" for k, v in señales.items()
    )

    prompt = f"""{SYSTEM_PROMPT}

SEÑALES TÉCNICAS (verificadas objetivamente, úsalas como evidencia):
{señales_txt}

CONTENIDO DEL EMAIL:
{texto_llm}

Devuelve únicamente el JSON según el esquema."""

    try:
        res = _ollama_call(OLLAMA_MODEL, prompt, CLASSIFICATION_SCHEMA)

        # Si la confianza es baja, reintentar con modelo mayor
        if res.get("confianza", 100) < 60:
            print("   ↪ Baja confianza, reintentando con modelo mayor...")
            try:
                res = _ollama_call(OLLAMA_FALLBACK_MODEL, prompt, CLASSIFICATION_SCHEMA)
            except Exception:
                pass  # si falla, nos quedamos con el primero

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
            print(f"  De:     {r['sender'][:80]}")
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
    print("═" * 60)
    print(" Clasificador de Email — Triaje enriquecido")
    print("═" * 60)

    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        print("\n[!] Faltan GMAIL_USER o GMAIL_APP_PASSWORD en el .env")
        return

    conn = init_db()
    emails = fetch_gmail_emails(GMAIL_USER, GMAIL_APP_PASSWORD, HOURS_TO_FETCH)

    if not emails:
        print("No hay correos que procesar.")
        return

    resultados = []
    for idx, item in enumerate(emails, 1):
        print(f"\n[{idx}/{len(emails)}] {item['subject'][:70]}")

        h = hash_email(item["texto_llm"])
        cached = cache_get(conn, h)

        if cached:
            print("   ✓ desde caché")
            clasificacion = cached
        else:
            clasificacion = classify_with_ollama(item["texto_llm"], item["señales"])
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


if __name__ == "__main__":
    main()