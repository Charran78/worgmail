import json
import sys
import numpy as np
import urllib.request
import urllib.error
import imaplib
import email
from email.header import decode_header
from datetime import datetime, timedelta

# Configuración optimizada para: Intel i5 (4 núcleos) + 2GB VRAM + 12GB RAM
CPU_THREADS = 4        # Coincide con los 4 núcleos físicos de un i5
CONTEXT_SIZE = 512     # Contexto ajustado para no desperdiciar VRAM
OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "qwen2.5:0.5b"  # Puedes probar "qwen2.5:1.5b" para mayor precisión

# Configuración de Gmail IMAP
GMAIL_USER = "beyond.digital.web@gmail.com"           # Tu dirección de correo
GMAIL_APP_PASSWORD = "fyxhiucykdptpiye"   # Tu Contraseña de Aplicación de 16 caracteres
HOURS_TO_FETCH = 48                          # Horas a revisar

# Configuración para llama-cpp-python directo
HF_REPO_ID = "Qwen/Qwen2.5-0.5B-Instruct-GGUF"
HF_FILENAME = "qwen2.5-0.5b-instruct-q8_0.gguf"

# Datos de prueba
LABELS = ["A", "B", "C"]
CHOICES = ["Legitimate", "Spam", "Phishing"]

# Nombres de meses estandarizados en inglés para IMAP (evita fallos de idioma/locale)
ENGLISH_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

def fetch_gmail_emails(username, app_password, hours=48):
    """
    Conecta a Gmail via IMAP y obtiene los correos recibidos en las ultimas 'hours' horas.
    """
    print(f"\n--- Conectando a Gmail IMAP ({username}) ---")
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        
        # Eliminar espacios accidentales en la contraseña de aplicación
        clean_password = app_password.replace(" ", "").strip()
        mail.login(username, clean_password)
        mail.select("inbox")

        # Calcular fecha IMAP estandarizada en inglés
        target_date = datetime.now() - timedelta(hours=hours)
        since_date = f"{target_date.day:02d}-{ENGLISH_MONTHS[target_date.month - 1]}-{target_date.year}"
        
        print(f"Buscando correos recibidos desde: {since_date}")
        status, messages = mail.search(None, f'(SINCE "{since_date}")')
        
        if status != "OK" or not messages[0]:
            print("No se encontraron correos en el periodo especificado.")
            mail.logout()
            return []

        email_ids = messages[0].split()
        print(f"Encontrados {len(email_ids)} correos recientes.")

        fetched_emails = []
        for e_id in email_ids:
            _, msg_data = mail.fetch(e_id, "(RFC822)")
            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    msg = email.message_from_bytes(response_part[1])
                    
                    # Decodificar el asunto
                    raw_subject = msg.get("Subject", "(Sin Asunto)")
                    subject_header = decode_header(raw_subject)[0]
                    if isinstance(subject_header[0], bytes):
                        encoding = subject_header[1] or 'utf-8'
                        try:
                            subject = subject_header[0].decode(encoding)
                        except (LookupError, UnicodeDecodeError):
                            subject = subject_header[0].decode('utf-8', errors='ignore')
                    else:
                        subject = str(subject_header[0])

                    sender = msg.get("From", "Desconocido")
                    
                    # Extraer cuerpo de texto plano
                    body = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            content_type = part.get_content_type()
                            if content_type == "text/plain":
                                try:
                                    body = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                                    break
                                except Exception:
                                    pass
                    else:
                        try:
                            body = msg.get_payload(decode=True).decode('utf-8', errors='ignore')
                        except Exception:
                            body = ""

                    clean_text = f"Subject: {subject}\nFrom: {sender}\nContent: {body[:600].strip()}"
                    fetched_emails.append({
                        "id": e_id.decode(),
                        "subject": subject,
                        "sender": sender,
                        "full_prompt_text": clean_text
                    })
        
        mail.logout()
        return fetched_emails

    except imaplib.IMAP4.error as e:
        print(f"\n[ERROR IMAP]: {e}")
        print("Si el error es de credenciales, asegúrate de haber generado la 'Contraseña de aplicación' en:")
        print("-> https://myaccount.google.com/apppasswords")
        return []
    except Exception as e:
        print(f"\n[ERROR GENERAL]: {e}")
        return []


def classify_with_ollama(email_text, choices):
    """
    Ejecuta la clasificación mediante el servidor Ollama usando JSON Estructurado.
    """
    print("\n--- [MODO OLLAMA] Clasificando con servidor Ollama local ---")
    
    prompt = f"""You are a cybersecurity expert. Analyze the following email carefully.
If it asks for credentials, passwords, or uses suspicious third-party links, it is Phishing.

Email content:
"{email_text}"

Classify this email strictly into one of these categories: {', '.join(choices)}."""

    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "format": {
            "type": "object",
            "properties": {
                "razon_breve": {
                    "type": "string"
                },
                "clasificacion": {
                    "type": "string",
                    "enum": choices
                }
            },
            "required": ["razon_breve", "clasificacion"]
        },
        "options": {
            "num_gpu": 99,
            "num_ctx": CONTEXT_SIZE,
            "num_thread": CPU_THREADS,
            "temperature": 0.0
        }
    }

    try:
        req = urllib.request.Request(
            OLLAMA_URL,
            data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
            method='POST'
        )
        
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode('utf-8'))
            response_json = json.loads(result.get('response', '{}'))
            return response_json

    except urllib.error.URLError as e:
        print(f"Error de conexión con Ollama: {e}")
        return None


def classify_with_llama_cpp(email_text, labels, choices):
    """
    Ejecuta el cálculo exacto de Logits y Probabilidades usando llama-cpp-python.
    """
    print("\n--- [MODO LLAMA-CPP] Calculando Logits y Probabilidades con llama-cpp-python ---")
    
    try:
        from llama_cpp import Llama
    except ImportError:
        print("Error: llama-cpp-python no está instalado.")
        return

    print(f"Cargando modelo {HF_REPO_ID} desde la caché local...")
    model = Llama.from_pretrained(
        repo_id=HF_REPO_ID,
        filename=HF_FILENAME,
        n_ctx=CONTEXT_SIZE,
        n_gpu_layers=-1,
        n_threads=CPU_THREADS,
        logits_all=True,
        verbose=False
    )

    options_formatted = "\n".join(
        f"{label}. {choice}" for label, choice in zip(labels, choices, strict=True)
    )

    prompt = f"""<|im_start|>system
You are a security filter. Choose only the single letter (A, B, or C) corresponding to the correct category.<|im_end|>
<|im_start|>user
Email: {email_text}

Categories:
{options_formatted}<|im_end|>
<|im_start|>assistant
Answer:"""

    tokens = model.tokenize(text=prompt.encode('utf-8'), add_bos=False, special=True)
    model.eval(tokens=tokens)

    scores = model.scores[model.n_tokens - 1]
    logits = np.array(scores, dtype=np.float64)

    choice_logits = []
    for label in labels:
        ids_to_check = []
        for fmt in [label, f" {label}", f"{label}."]:
            toks = model.tokenize(text=fmt.encode('utf-8'), add_bos=False)
            if len(toks) > 0:
                ids_to_check.append(toks[0])
        
        valid_scores = [logits[idx] for idx in ids_to_check if idx < len(logits)]
        choice_logits.append(max(valid_scores) if valid_scores else -100.0)

    choice_logits_np = np.asarray(choice_logits, dtype=np.float64)
    
    logprobs = choice_logits_np - np.logaddexp.reduce(choice_logits_np)
    probabilities = np.exp(logprobs)

    for name, scores in (
        ("Logits", choice_logits_np),
        ("Log probabilities", logprobs),
        ("Probabilidades", probabilities),
    ):
        values = np.round(scores.astype(float), 4).tolist()
        print(f"{name:18}:", dict(zip(choices, values, strict=True)))


if __name__ == "__main__":
    print("=======================================================")
    print(" Clasificador de Email Optimizado (2GB GPU / 12GB RAM) ")
    print("=======================================================")

    IS_CONFIGURED = (
        GMAIL_USER not in ("", "tu_correo@gmail.com") 
        and GMAIL_APP_PASSWORD not in ("", "xxxx xxxx xxxx xxxx")
    )

    if IS_CONFIGURED:
        emails = fetch_gmail_emails(GMAIL_USER, GMAIL_APP_PASSWORD, HOURS_TO_FETCH)
        print(f"\nProcesando {len(emails)} correos con Ollama...")
        
        for idx, item in enumerate(emails, 1):
            print(f"\n================ Correo [{idx}/{len(emails)}] ================")
            print(f"De:     {item['sender']}")
            print(f"Asunto: {item['subject']}")
            
            res = classify_with_ollama(item["full_prompt_text"], CHOICES)
            if res:
                print(f"-> DICTAMEN: [{res.get('clasificacion')}] - {res.get('razon_breve')}")
    else:
        print("\n[MODO PRUEBA - SCRIPT MOCKUP]")
        print("Para usar tu Gmail real, edita las variables GMAIL_USER y GMAIL_APP_PASSWORD en el script.")
        
        SAMPLE_EMAIL = "Payroll asks for your password on a non-company sign-in page."
        print(f"\nEmail a analizar: '{SAMPLE_EMAIL}'")
        classify_with_ollama(SAMPLE_EMAIL, CHOICES)
        classify_with_llama_cpp(SAMPLE_EMAIL, LABELS, CHOICES)