# API Contract — `vet_agents_telemetry`

**Versione documento:** 0.1
**Data:** 12 maggio 2026
**Owner:** Tommaso Collarile
**Stato:** contratto definitivo per implementazione v0.1
**Documenti padre:** `project_state.md` v0.3, `architecture.md` v0.1

---

## 0. Scopo del documento

Definire il contratto preciso della libreria `vet_agents_telemetry`: firme delle funzioni, vocabolari standard, schema dei `metadata`, esempi d'uso. È **la fonte di verità** che ogni agente nuovo consulta in fase di integrazione.

Tutto ciò che è qui dentro deve restare stabile. Modifiche al contratto = bump versione libreria + migrazione esplicita degli agenti.

---

## 1. API pubblica — sommario

```python
from vet_agents_telemetry import init, log_event, heartbeat, log_usage
```

| Funzione | Quando si chiama | Frequenza tipica |
|---|---|---|
| `init()` | All'avvio dell'agente, una volta sola | 1 / processo |
| `log_event()` | Quando succede qualcosa di degno di nota | 1-100 / giorno |
| `heartbeat()` | "Sono vivo" (manuale o automatico) | 1 / 5 min |
| `log_usage()` | Dopo OGNI chiamata API a un provider AI | 1 / chiamata API |

---

## 2. `init()`

### 2.1 Firma

```python
def init(
    agent_id: str,
    *,
    auto_heartbeat: bool = True,
    heartbeat_interval_sec: int = 300,
) -> None
```

### 2.2 Parametri

| Param | Tipo | Default | Note |
|---|---|---|---|
| `agent_id` | `str` | obbligatorio | kebab-case, identico al nome repo (es. `pdf-analisi-archive`) |
| `auto_heartbeat` | `bool` | `True` | Se `True`, avvia thread daemon che chiama `heartbeat()` automaticamente |
| `heartbeat_interval_sec` | `int` | `300` | Intervallo in secondi tra heartbeat automatici |

### 2.3 Effetti collaterali

1. Legge env vars (`TELEMETRY_SUPABASE_URL`, `TELEMETRY_SUPABASE_KEY`, `TELEMETRY_ENABLED`)
2. Inizializza singleton client Supabase
3. Logga evento `agent_started` (vedi §5.1)
4. Esegue `heartbeat()` iniziale
5. Se `auto_heartbeat=True`, avvia thread daemon

### 2.4 Esempio

```python
# All'avvio del processo agente
from vet_agents_telemetry import init

init(agent_id="pdf-analisi-archive")
# Da qui in poi log_event, heartbeat, log_usage funzionano
```

### 2.5 Fallimenti

- Se `TELEMETRY_ENABLED=false` → no-op silenzioso, ritorna senza errori
- Se env vars mancanti → log warning su `logging`, no-op silenzioso
- Mai re-raise

---

## 3. `log_event()`

### 3.1 Firma

```python
def log_event(
    type: str,
    *,
    tenant_id: str = "self",
    severity: Literal["info", "warning", "error"] = "info",
    message: str | None = None,
    metadata: dict | None = None,
) -> None
```

### 3.2 Parametri

| Param | Tipo | Default | Note |
|---|---|---|---|
| `type` | `str` | obbligatorio | snake_case. Standard (§5) o custom dell'agente |
| `tenant_id` | `str` | `"self"` | Identificatore tenant. Stringa, mai null |
| `severity` | enum | `"info"` | `info` / `warning` / `error` |
| `message` | `str \| None` | `None` | Descrizione human-readable, opzionale. Max 500 char raccomandato |
| `metadata` | `dict \| None` | `None` | Solo ID di riferimento, mai payload completi. Vedi §6 |

### 3.3 Esempi

```python
# Evento info standard
log_event("processing_started", metadata={"job_id": "j-12345"})

# Evento custom dell'agente
log_event(
    "pdf_processed",
    tenant_id="anna-de-nitto",
    metadata={
        "pdf_filename": "rossi_micio_2026-05-08.pdf",
        "patient_id": "abc123",
        "pages": 4,
    }
)

# Evento di errore
log_event(
    "pdf_processing_failed",
    tenant_id="anna-de-nitto",
    severity="error",
    message="Patient not found in Notion",
    metadata={
        "pdf_filename": "gattino_orfano.pdf",
        "reason": "patient_not_found",
    }
)
```

---

## 4. `heartbeat()`

### 4.1 Firma

```python
def heartbeat(
    status: Literal["ok", "degraded", "offline"] = "ok",
) -> None
```

### 4.2 Parametri

| Param | Tipo | Default | Note |
|---|---|---|---|
| `status` | enum | `"ok"` | `ok` / `degraded` / `offline` |

### 4.3 Quando usare `degraded` vs `offline`

- `ok` — tutto funziona, default
- `degraded` — l'agente è vivo ma sa di avere un problema (es. dipendenza esterna down, performance degradata)
- `offline` — l'agente sta per spegnersi volontariamente (es. shutdown gracioso)

**Nota:** se l'agente crasha, non riuscirà a inviare `offline`. La dashboard rileva il crash dall'assenza di heartbeat → status visivo "rosso" calcolato lato dashboard.

### 4.4 Esempi

```python
# Caso normale (in genere automatico via init(auto_heartbeat=True))
heartbeat()

# Modalità degradata
if not external_service.is_healthy():
    heartbeat(status="degraded")

# Shutdown gracioso
def shutdown():
    heartbeat(status="offline")
    log_event("agent_stopped")
```

---

## 5. Vocabolario eventi standard cross-agente

Questi sei `type` sono riservati e devono essere usati con la semantica qui definita. Permettono alla dashboard di mostrare statistiche uniformi senza conoscere ogni agente.

### 5.1 `agent_started`

Emesso automaticamente da `init()`. Non chiamarlo manualmente.

- `severity`: `info`
- `metadata` raccomandato: `{ "version": "0.1.0", "host": "vps-agenti-01", "pid": 12345 }`

### 5.2 `agent_stopped`

Shutdown volontario e pulito.

- `severity`: `info`
- `metadata` opzionale: `{ "reason": "scheduled_restart" }`

### 5.3 `agent_crashed`

Crash gestito (catturato da un handler globale prima della morte del processo).

- `severity`: `error`
- `metadata` raccomandato: `{ "exception": "ValueError", "traceback_summary": "..." }`

### 5.4 `processing_started`

Inizio di un'unità di lavoro (un PDF, un'email, una richiesta).

- `severity`: `info`
- `metadata` raccomandato: `{ "job_id": "...", "input_ref": "..." }`

### 5.5 `processing_completed`

Fine con successo dell'unità di lavoro precedente.

- `severity`: `info`
- `metadata` raccomandato: `{ "job_id": "...", "duration_ms": 4200, "output_ref": "..." }`

### 5.6 `processing_failed`

L'unità di lavoro è fallita.

- `severity`: `error`
- `metadata` raccomandato: `{ "job_id": "...", "reason": "...", "exception": "..." }`

### 5.7 Coppia `job_id`

Lo stesso `job_id` su `processing_started` + `processing_completed`/`processing_failed` permette di calcolare durate, tassi di successo, ecc.

Convenzione `job_id`: stringa libera, ma deve essere unica per job. Esempio: `f"{agent_id}-{uuid4().hex[:8]}"` o un ID naturale del dominio (es. message ID email).

---

## 6. `metadata` — convenzioni

### 6.1 Regole generali

1. **Solo riferimenti, mai contenuti**. ID, filename, count, timestamp, flag. Mai testi completi, mai contenuti di email, mai prompt, mai response LLM.
2. **Snake_case per le chiavi**.
3. **Tipi serializzabili JSON**: string, number, boolean, array di scalar, null. Niente datetime nativi → ISO 8601 string.
4. **Profondità max 2 livelli**. Niente JSON annidati complessi.
5. **Dimensione raccomandata < 2 KB**. La dashboard mostra il `metadata` come tabella chiave-valore.

### 6.2 Chiavi standard riservate

Convenzioni cross-agente raccomandate:

| Chiave | Tipo | Semantica |
|---|---|---|
| `job_id` | str | Identificatore unità di lavoro |
| `duration_ms` | int | Durata in millisecondi |
| `reason` | str | Causa human-readable di un fallimento (snake_case) |
| `exception` | str | Nome classe eccezione |
| `traceback_summary` | str | Ultime 3-5 righe stack trace |
| `input_ref` | str | Riferimento all'input (filename, message_id, url) |
| `output_ref` | str | Riferimento all'output (notion_page_id, file path) |
| `host` | str | Hostname server dove gira l'agente |
| `pid` | int | Process ID |
| `version` | str | Versione agente o libreria |

Chiavi specifiche dell'agente: libere, snake_case, documentate nel `CLAUDE.md` dell'agente.

### 6.3 Esempi corretti vs sbagliati

✅ **Corretto**
```python
metadata={
    "pdf_filename": "rossi_micio_2026-05-08.pdf",
    "patient_id": "abc123",
    "pages": 4,
    "duration_ms": 1850,
}
```

❌ **Sbagliato** — payload, troppo grande, sensibile
```python
metadata={
    "pdf_content": "<<< 50KB di testo estratto >>>",
    "llm_response": "<<< intera response Claude >>>",
    "patient_full_record": {...annidato...},
}
```

---

## 7. `log_usage()`

### 7.1 Firma

```python
def log_usage(
    provider: Literal["anthropic", "openai", "openrouter"],
    model: str,
    operation: str,
    *,
    tenant_id: str = "self",
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    units: float | None = None,
    unit_type: str | None = None,
    cost_usd: float | None = None,
) -> None
```

### 7.2 Parametri

| Param | Tipo | Default | Note |
|---|---|---|---|
| `provider` | enum | obbligatorio | `anthropic` / `openai` / `openrouter` |
| `model` | `str` | obbligatorio | Identificatore esatto modello come riportato dal provider |
| `operation` | `str` | obbligatorio | snake_case, etichetta business (vedi §8) |
| `tenant_id` | `str` | `"self"` | Identificatore tenant |
| `input_tokens` | `int \| None` | `None` | Per modelli token-based |
| `output_tokens` | `int \| None` | `None` | Per modelli token-based |
| `units` | `float \| None` | `None` | Per modelli unit-based (audio sec, char TTS) |
| `unit_type` | `str \| None` | `None` | Es. `seconds`, `characters`, `images` |
| `cost_usd` | `float \| None` | `None` | Se passato, usato direttamente. Altrimenti calcolato via pricing table |

### 7.3 Regole di valorizzazione

Almeno UNA tra `(input_tokens, output_tokens)` o `(units, unit_type)` deve essere valorizzata.

Per **OpenRouter**: il costo arriva nella response API. Passare `cost_usd` esplicito e omettere il calcolo automatico. Esempio:
```python
response = openrouter_client.chat(...)
log_usage(
    provider="openrouter",
    model=response.model,
    operation="email_classification",
    input_tokens=response.usage.prompt_tokens,
    output_tokens=response.usage.completion_tokens,
    cost_usd=response.usage.cost,  # OpenRouter lo dichiara
)
```

### 7.4 Esempi

```python
# Anthropic — token based, costo calcolato dalla pricing table
response = anthropic_client.messages.create(...)
log_usage(
    provider="anthropic",
    model="claude-sonnet-4-6",
    operation="pdf_classification",
    tenant_id="anna-de-nitto",
    input_tokens=response.usage.input_tokens,
    output_tokens=response.usage.output_tokens,
)

# OpenAI Whisper — unit based (secondi di audio)
log_usage(
    provider="openai",
    model="whisper-1",
    operation="voice_transcription",
    units=audio_duration_sec,
    unit_type="seconds",
)

# OpenAI TTS — unit based (caratteri)
log_usage(
    provider="openai",
    model="tts-1",
    operation="tts_generation",
    units=len(text),
    unit_type="characters",
)
```

---

## 8. Vocabolario `operation`

### 8.1 Regole

- snake_case obbligatorio
- Descrive il *cosa* a livello business, non il *come* tecnico
- Lista aperta: ogni agente definisce le sue
- Documentate nel `CLAUDE.md` dell'agente

### 8.2 Esempi raccomandati per gli agenti esistenti / pianificati

| Agente | `operation` |
|---|---|
| `pdf-analisi-archive` | `pdf_classification`, `pdf_summarization`, `email_extraction` |
| `agente-rx-notion` | `patient_matching`, `image_description`, `metadata_extraction` |
| `vetmail-triage` | `email_classification`, `priority_scoring`, `reply_drafting` |
| `vethistory-ai` | `behavioral_extraction`, `clinical_synthesis`, `report_generation` |

Valori vincolanti? **No**, sono raccomandazioni. Ma se due agenti fanno la stessa operazione, dovrebbero usare lo stesso `operation` per permettere confronti aggregati.

---

## 9. Comportamento fail-silent

### 9.1 Principio

**Nessuna chiamata della libreria può causare crash dell'agente.** Mai. Punto.

### 9.2 Modalità di fallimento e gestione

| Situazione | Comportamento libreria |
|---|---|
| Supabase irraggiungibile | log WARNING locale, ritorna `None`, agente continua |
| Timeout (>3s) | log WARNING locale, ritorna `None` |
| Auth failure (chiave revocata) | log ERROR locale, ritorna `None` |
| Schema invalido (campo nuovo non riconosciuto) | log ERROR locale, ritorna `None` |
| `TELEMETRY_ENABLED=false` | No-op completo, nessun log |
| `init()` mai chiamato | log ERROR locale ("init() not called"), ritorna `None` |
| Pricing model non trovato | log evento `pricing_missing` con `severity=warning`, salva `cost_usd=0` |

### 9.3 Log locali

La libreria usa il modulo standard `logging` con logger nominato `vet_agents_telemetry`. L'agente può configurarlo a piacere:

```python
import logging
logging.getLogger("vet_agents_telemetry").setLevel(logging.WARNING)
```

---

## 10. Pattern di integrazione consigliato

### 10.1 Boilerplate minimo per un nuovo agente

```python
import os
import logging
from vet_agents_telemetry import init, log_event, log_usage

# 1. Setup logging (la libreria usa logging standard)
logging.basicConfig(level=logging.INFO)

# 2. Init all'avvio
init(agent_id="my-new-agent")

# 3. Handler errori globale
def handle_crash(exception):
    log_event(
        "agent_crashed",
        severity="error",
        metadata={
            "exception": type(exception).__name__,
            "traceback_summary": str(exception)[:500],
        }
    )

# 4. Per ogni unità di lavoro
def process_job(job):
    log_event("processing_started", metadata={"job_id": job.id})
    try:
        result = do_work(job)
        log_event(
            "processing_completed",
            metadata={"job_id": job.id, "duration_ms": result.duration_ms}
        )
    except Exception as e:
        log_event(
            "processing_failed",
            severity="error",
            metadata={"job_id": job.id, "reason": str(e)[:200]}
        )
        raise

# 5. Dopo ogni chiamata API LLM
response = anthropic.messages.create(model="claude-sonnet-4-6", ...)
log_usage(
    provider="anthropic",
    model="claude-sonnet-4-6",
    operation="my_specific_operation",
    input_tokens=response.usage.input_tokens,
    output_tokens=response.usage.output_tokens,
)
```

### 10.2 Wrapping di un client API

Pattern raccomandato per non sparpagliare `log_usage()` in tutto il codice:

```python
# nel modulo dell'agente
class TrackedAnthropic:
    def __init__(self, client, operation: str, tenant_id: str = "self"):
        self.client = client
        self.operation = operation
        self.tenant_id = tenant_id

    def messages_create(self, **kwargs):
        response = self.client.messages.create(**kwargs)
        log_usage(
            provider="anthropic",
            model=kwargs["model"],
            operation=self.operation,
            tenant_id=self.tenant_id,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        return response
```

Lo strato di astrazione protegge il codice futuro (coerente con `find_patient_page()` di agente-rx-notion).

---

## 11. Versioning del contratto

| Cambiamento | Bump | Compatibilità |
|---|---|---|
| Nuovo parametro opzionale | minor (0.X.Y → 0.X+1.0) | Backward compatible |
| Nuovo `type` standard | minor | Backward compatible |
| Nuovo `provider` enum | minor | Backward compatible (richiede migration SQL) |
| Rimozione parametro | major (0.X.Y → 1.0.0 se da 0.x) | Breaking |
| Cambio semantica `type` standard | major | Breaking |
| Cambio firma funzione | major | Breaking |

Modifiche al contratto **devono** passare prima da una proposta scritta (PR sul repo libreria + aggiornamento di questo documento) prima dell'implementazione.

---

## 12. Definition of done — `api_contract.md`

- [x] Firme delle 4 funzioni pubbliche definite con tipi precisi
- [x] Vocabolario eventi standard cross-agente definito (6 type)
- [x] Convenzioni `metadata` definite (regole + chiavi standard)
- [x] Vocabolario `operation` con esempi per agenti esistenti/pianificati
- [x] Comportamento fail-silent specificato esaustivamente
- [x] Pattern di integrazione documentato (boilerplate + wrapping API)
- [x] Policy di versioning del contratto definita

Pronto per `CLAUDE.md` operativo (istruzioni per Claude Code in fase di implementazione).
