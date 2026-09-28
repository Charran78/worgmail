"""
Clasificador de email con triaje enriquecido — v7
- Switch manual de modelo vía .env (OLLAMA_MODEL)
- Few-shot examples para mejorar la calidad del 0.5b
- Whitelist separada: solo newsletters PURAS se saltan el LLM
- Schema reducido + señales compactas → menos tiempo por llamada
- Detección de GPU/CPU en la precarga
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

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

GMAIL_USER = (os.getenv("GMAIL_USER") or "").strip()
GMAIL_APP_PASSWORD = (os.getenv("GMAIL_APP_PASSWORD") or "").strip().strip('"').strip("'")

HOURS_TO_FETCH = int(os.getenv("HOURS_TO_FETCH", "48"))

# ⚡ Modelo: se elige desde el .env. Ejemplos válidos:
#    OLLAMA_MODEL=qwen2.5:0.5b     (rápido, calidad media)
#    OLLAMA_MODEL=qwen2.5:1.5b     (más lento, calidad alta)
#    OLLAMA_MODEL=qwen2.5:3b       (muy lento en 2GB VRAM, solo si tienes tiempo)
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:0.5b")

CPU_THREADS = int(os.getenv("CPU_THREADS", "4"))
CONTEXT_SIZE = int(os.getenv("CONTEXT_SIZE", "512"))
OLLAMA_URL = "http://localhost:11434/api/generate"
KEEP_ALIVE = "2h"
NUM_PREDICT = int(os.getenv("NUM_PREDICT", "180"))
OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "180"))

DB_PATH = BASE_DIR / "cache_emails.db"

ENGLISH_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# ⚡ Solo dominios que SIEMPRE son newsletters. Estos se saltan el LLM.
# Los dominios "mixtos" (linkedin, github, supabase, malt, etc.) van al LLM
# porque pueden enviar Trabajo/Notificacion importantes.
REMITENTES_NEWSLETTER_PUROS = {
    "substack.com", "beehiiv.com", "producthunt.com",
    "medium.com", "dev.to", "hashnode.com",
    "inoreader.com", "codepen.io",
    "mail.beehiiv.com", "deeperlearning.producthunt.com",
}

# ══════════════════════════════════════════════════════════════════════
# ESQUEMA Y PROMPT
# ══════════════════════════════════════════════════════════════════════

CATEGORIAS = [
    "Newsletter", "Notificacion", "Transaccional", "Trabajo",
    "Personal", "Promocion", "Spam", "Phishing"
]

# ⚡ Schema reducido (sin plazo ni razon_breve, que alargaban la generación)
CLASSIFICATION_SCHEMA = {
    "type": "object",
    "properties": {
        "resumen":            {"type": "string"},
        "categoria":          {"type": "string", "enum": CATEGORIAS},
        "importancia":        {"type": "integer", "minimum": 1, "maximum": 5},
        "urgencia":           {"type": "string", "enum": ["baja", "media", "alta"]},
        "requiere_respuesta": {"type": "boolean"},
        "requiere_accion":    {"type": "boolean"},
        "accion_sugerida":    {"type": "string"},
        "riesgo_seguridad":   {"type": "string", "enum": ["ninguno", "bajo", "medio", "alto"]},
        "confianza":          {"type": "integer", "minimum": 0, "maximum": 100},
    },
    "required": [
        "resumen", "categoria", "importancia", "urgencia",
        "requiere_respuesta", "requiere_accion", "accion_sugerida",
        "riesgo_seguridad", "confianza"
    ]
}

# ⚡ Prompt con few-shot. El modelo pequeño copia el patrón de los ejemplos.
SYSTEM_PROMPT = """Clasificas emails. Devuelves SOLO JSON válido, sin texto extra.

CATEGORÍAS: Newsletter, Notificacion, Transaccional, Trabajo, Personal, Promocion, Spam, Phishing.

REGLAS:
- "Phishing" SOLO con evidencia: dominio falso, urgencia pidiendo credenciales, links que no coinciden con el remitente.
- Alertas de accounts.google.com → "Notificacion", riesgo "bajo".
- Ofertas de empleo (aunque vengan de LinkedIn o portales) → "Trabajo".
- Alertas de servicios (proyecto pausado, cuota, seguridad, facturación) → "Notificacion", importancia 3.
- Importancia: 1=ruido, 3=normal, 5=crítico (dinero/seguridad/trabajo urgente).
- NO INVENTES DATOS. Si no lo ves en el email, no lo pongas.

EJEMPLOS:

--- EMAIL ---
From: no-reply@substack.com
Subject: New post from Dan
--- JSON ---
{"resumen": "Nuevo articulo en Substack", "categoria": "Newsletter", "importancia": 1, "urgencia": "baja", "requiere_respuesta": false, "requiere_accion": false, "accion_sugerida": "Ninguna", "riesgo_seguridad": "ninguno", "confianza": 90}

--- EMAIL ---
From: no-reply@accounts.google.com
Subject: Alerta de seguridad
--- JSON ---
{"resumen": "Alerta de seguridad de Google", "categoria": "Notificacion", "importancia": 3, "urgencia": "baja", "requiere_respuesta": false, "requiere_accion": true, "accion_sugerida": "Revisar actividad reciente en Google", "riesgo_seguridad": "bajo", "confianza": 90}

--- EMAIL ---
From: jobs-noreply@linkedin.com
Subject: Oferta de empleo en Madrid
--- JSON ---
{"resumen": "Oferta de empleo en Madrid", "categoria": "Trabajo", "importancia": 2, "urgencia": "baja", "requiere_respuesta": false, "requiere_accion": false, "accion_sugerida": "Revisar oferta", "riesgo_seguridad": "ninguno", "confianza": 88}

--- EMAIL ---
From: ant.wilson@supabase.com
Subject: Your project is going to be paused
--- JSON ---
{"resumen": "Proyecto Supabase se pausara", "categoria": "Notificacion", "importancia": 3, "urgencia": "media", "requiere_respuesta": false, "requiere_accion": true, "accion_sugerida": "Reactivar proyecto en Supabase", "riesgo_seguridad": "bajo", "confianza": 85}
"""

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


def extraer_cuerpo(msg, max_chars=1000):
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
    try:
        return msg.get_payload(decode=True).decode("utf-8", errors="ignore")[:max_chars]
    except Exception:
        return ""


def analizar_señales(msg):
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

    if dominios_enlaces and dominio_from != "desconocido":
        raiz = ".".join(dominio_from.split(".")[-2:])
        señales["mismatch_dominio"] = not any(raiz in d for d in dominios_enlaces)
    else:
        señales["mismatch_dominio"] = False

    return señales


def señales_compactas(señales):
    """⚡ Solo las señales que aportan al LLM pequeño."""
    return {
        "dominio":           señales.get("dominio_remitente"),
        "spf":               señales.get("spf"),
        "mismatch_dominio":  señales.get("mismatch_dominio"),
        "reply_to_distinto": señales.get("reply_to_distinto"),
    }


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

                cuerpo = extraer_cuerpo(msg, max_chars=1000)
                señales = analizar_señales(msg)

                texto_llm = (
                    f"From: {sender}\n"
                    f"Subject: {subject}\n"
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
# PRE-FILTRO
# ══════════════════════════════════════════════════════════════════════

def pre_clasificar(sender):
    """Solo saltamos el LLM para newsletters PURAS."""
    m = re.search(r"@([\w\.\-]+)", sender)
    if not m:
        return None
    dominio = m.group(1).lower()

    # Compara el dominio completo y la raíz
    raiz = ".".join(dominio.split(".")[-2:])
    if dominio in REMITENTES_NEWSLETTER_PUROS or raiz in REMITENTES_NEWSLETTER_PUROS:
        return {
            "resumen": "Newsletter de remitente conocido",
            "categoria": "Newsletter",
            "importancia": 1,
            "urgencia": "baja",
            "requiere_respuesta": False,
            "requiere_accion": False,
            "accion_sugerida": "Ninguna",
            "riesgo_seguridad": "ninguno",
            "confianza": 95,
        }
    return None

# ══════════════════════════════════════════════════════════════════════
# OLLAMA
# ══════════════════════════════════════════════════════════════════════

def _ollama_call(modelo, prompt, schema):
    payload = {
        "model": modelo,
        "prompt": prompt,
        "stream": False,
        "format": schema,
        "keep_alive": KEEP_ALIVE,
        "options": {
            "num_gpu": 99,
            "num_ctx": CONTEXT_SIZE,
            "num_thread": CPU_THREADS,
            "temperature": 0.0,
            "num_predict": NUM_PREDICT,
            "top_k": 1,
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


def _detectar_procesador():
    """⚡ Consulta /api/ps para ver si el modelo corre en GPU o CPU."""
    try:
        req = urllib.request.Request("http://localhost:11434/api/ps")
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
            for m in data.get("models", []):
                proc = m.get("size_vram", 0)
                total = m.get("size", 1)
                pct = int(100 * proc / total) if total else 0
                return f"GPU {pct}% / CPU {100-pct}%"
    except Exception:
        pass
    return "desconocido"


def classify_with_ollama(texto_llm, señales):
    señales_txt = "\n".join(f"- {k}: {v}" for k, v in señales_compactas(señales).items())

    prompt = f"""{SYSTEM_PROMPT}

--- EMAIL ---
{texto_llm}

Señales técnicas:
{señales_txt}

--- JSON ---"""

    try:
        return _ollama_call(OLLAMA_MODEL, prompt, CLASSIFICATION_SCHEMA)
    except urllib.error.URLError as e:
        print(f"[ERROR OLLAMA] {e}")
        return None
    except Exception as e:
        print(f"[ERROR CLASIFICANDO] {e}")
        return None

# ══════════════════════════════════════════════════════════════════════
# PRESENTACIÓN
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
            print(f"  Conf:    {c.get('confianza', '?')}")

    if not hay:
        print("\nNo hay acciones pendientes. 🎉")


def imprimir_resumen_ejecutivo(resultados):
    total = len(resultados)
    por_cat = {}
    riesgos = respuestas = acciones = 0

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
    print(" Clasificador de Email — Triaje v7")
    print(f" Modelo: {OLLAMA_MODEL}")
    print("═" * 60)

    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        print("\n[!] Faltan GMAIL_USER o GMAIL_APP_PASSWORD en el .env")
        return

    preload_model(OLLAMA_MODEL)
    print(f"[Procesador] {_detectar_procesador()}")

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
            clasificacion = pre_clasificar(item["sender"])

            if clasificacion:
                print("   ⚡ pre-filtro newsletter")
                n_heuristic += 1
            else:
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