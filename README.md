# msg-triage (VetTriage v0)

Bot Telegram di **triage** per le conversazioni WhatsApp gestite via Callbell in una
clinica veterinaria specializzata in esotici e aviari. A comando recupera le conversazioni
recenti, le passa a Claude per un triage a tre livelli e restituisce su Telegram tre output
(vocale, schema, tabella). Non risponde ai messaggi: legge e riassume soltanto.

Il dettaglio di prodotto e architettura è in `docs/` (`project_state.md`, `dev_notes.md`,
`tasks.md`, `triage_system_prompt.md`).

## Requisiti
- Python 3.12+
- [uv](https://docs.astral.sh/uv/)

## Setup
```bash
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e ".[dev]"
cp .env.example .env   # poi compila i valori
```

Opzionale — telemetria (libreria pinnata a un tag, fuori da `pyproject.toml`; senza,
l'app funziona identica e la telemetria resta spenta). Dettagli in `docs/runbook.md § F`:
```bash
uv pip install git+ssh://git@github.com/Tomtokio/vet-agents-telemetry.git@v0.1.1
```

## Avvio
```bash
python -m msg_triage      # oppure: msg-triage
```
All'avvio legge i segreti dall'ambiente (o da `.env`, se presente) e logga `VetTriage ready`.
Se manca una variabile richiesta, si ferma elencando quelle mancanti.

## Test
```bash
pytest
```

## Stato
T1 (setup progetto e segreti) completato. I moduli successivi — source adapter Callbell,
motore di triage, memoria/Supabase, renderer, TTS, bot Telegram — arrivano con T2–T9
(vedi `docs/tasks.md`).
