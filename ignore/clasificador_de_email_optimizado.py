import json
import sys
import numpy as np
import urllib.request
import urllib.error

# Configuración optimizada para: Intel i5 (4 núcleos) + 2GB VRAM + 12GB RAM
CPU_THREADS = 4        # Coincide con los 4 núcleos físicos de un i5
CONTEXT_SIZE = 512     # Contexto ajustado para no desperdiciar VRAM
OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "qwen2.5:0.5b"  # Puedes probar "qwen2.5:1.5b" para mayor precisión

# Configuración para llama-cpp-python directo
HF_REPO_ID = "Qwen/Qwen2.5-0.5B-Instruct-GGUF"
HF_FILENAME = "qwen2.5-0.5b-instruct-q8_0.gguf"

# Datos de prueba
EMAIL_SAMPLE = "Payroll asks for your password on a non-company sign-in page."
LABELS = ["A", "B", "C"]
CHOICES = ["Legitimate", "Spam", "Phishing"]


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
                "clasificacion": {
                    "type": "string",
                    "enum": choices
                },
                "razon_breve": {
                    "type": "string"
                }
            },
            "required": ["clasificacion", "razon_breve"]
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
            print("Respuesta de Ollama:")
            print(json.dumps(response_json, indent=2, ensure_ascii=False))
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
        logits_all=True,  # Activado para obtener el vector completo de probabilidades
        verbose=False
    )

    options_formatted = "\n".join(
        f"{label}. {choice}" for label, choice in zip(labels, choices, strict=True)
    )

    # Prompt directo optimizado
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

    # Obtenemos los logits del último token evaluado
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
    
    # Normalización Softmax
    logprobs = choice_logits_np - np.logaddexp.reduce(choice_logits_np)
    probabilities = np.exp(logprobs)

    # Impresión de resultados
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
    print(f"Email a analizar: '{EMAIL_SAMPLE}'")

    classify_with_ollama(EMAIL_SAMPLE, CHOICES)
    classify_with_llama_cpp(EMAIL_SAMPLE, LABELS, CHOICES)