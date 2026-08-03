# CLAUDE.md — msg-triage

## Cos'è questo progetto
Triage intelligente delle conversazioni WhatsApp della clinica (via Callbell).
Legge i messaggi recenti, li giudica, produce un digest a tre livelli.
**Non risponde mai ai clienti.** Il triage è sola lettura; l'unica scrittura che il progetto
si concede è sui *tag* dei contatti (pulizia una tantum, poi T10). Mai un messaggio, mai
niente che il cliente possa vedere.

## Documentazione — LEGGILA PRIMA DI QUALSIASI COSA
- `docs/project_state.md` — cos'è, obiettivi, decisioni prese
- `docs/tasks.md` — task T1–T9, ordine e dipendenze
- `docs/dev_notes.md` — convenzioni, vincoli, anti-pattern
- `docs/triage_system_prompt.md` — il system prompt del triage (testo operativo)
- `docs/telemetry_api_contract.md` — contratto della libreria `vet_agents_telemetry`

## Protocollo di lavoro (due pause)
1. **Prima di scrivere codice**: proponi un piano e aspetta approvazione esplicita.
2. **Prima di commit/PR**: mostra cosa hai fatto e aspetta review.
Non saltare queste pause. Mai.

## Regole non negoziabili
- Un concern per PR/commit. Squash merge, branch cancellato dopo.
- **Deterministico prima di inferenza**: valida con logica/schemi prima di chiamare l'LLM.
- YAGNI. È uno strumento personale, non un prodotto: niente over-engineering.
- Codice e nomi in **inglese**. Stringhe rivolte all'utente in **italiano**.
- Segreti solo da variabili d'ambiente. Mai hardcoded, mai committati.
- **Non fare mai deploy, migrazioni DB, git tag o modifiche a env vars**: quelle le fa Tommaso a mano.
- Non modificare mai codice in produzione direttamente sul VPS.

## Vincoli architetturali del progetto
- **Source adapter**: il triage engine non deve MAI vedere strutture dati Callbell-specifiche.
  Passa sempre dal formato conversazione neutro. È ciò che rende il triage portabile.
- **Una sola chiamata LLM per triage**, output JSON strutturato. I tre formati (vocale/schema/
  tabella) si generano da quell'unico oggetto, non con tre chiamate.
- **Paletto etico**: il triage descrive lo stato delle conversazioni, non giudica l'operato
  delle colleghe. Vincolo non negoziabile, vedi dev_notes.

## Ambiente
- Python 3.12+ (`requires-python >= 3.12`)
- `uv` per le dipendenze. Nuovo workspace Conductor = venv da ricreare:
  `uv venv --python 3.12` poi `uv pip install -e ".[dev]"`
- Test: `.venv/bin/python -m pytest`
- Deploy target: VPS `vps-agenti` (systemd). Il deploy lo fa Tommaso.

## Telemetria (`vet_agents_telemetry` v0.1.1)
Un solo punto di import: **`msg_triage/telemetry.py`**. Nessun altro modulo importa la
libreria. Il wrapper è fail-silent e con import protetto: se la libreria non è installata
(non è in `pyproject.toml`, si pinna a un tag — vedi `docs/runbook.md § F`) tutto è no-op.

- **`tenant_id` = `"self"` sempre**, eventi di business inclusi. È lo strumento personale
  di Tommaso, non un servizio consegnato a un cliente: non c'è un tenant a cui attribuire
  niente. Diverso dagli altri agenti dell'ecosistema — non copiarli su questo punto.
- **`telemetry.setup()` va dopo il caricamento del `.env`**, altrimenti la libreria non
  trova le sue variabili. Le tre `TELEMETRY_*` sono sue: non riusa `SUPABASE_URL/KEY`.
- In contesto async si usa `await telemetry.aevent(...)`: la scrittura è HTTP bloccante
  (timeout 3 s) e bloccherebbe l'event loop. Nel worker thread va bene `telemetry.event(...)`.

**Eventi.** Standard: `agent_started` (dalla libreria), `agent_stopped`, `agent_crashed`,
`processing_started`, `processing_completed`, `processing_failed` — la coppia
started/completed|failed copre **ogni** run, finestra vuota inclusa (è un run riuscito che
non aveva niente da dire). Custom di questo agente: `conversations_fetched`,
`triage_judged`, `bot_error`.

**Severity.** `error` = richiede attenzione, `warning` = anomalia già rientrata. Emettono
`error` **soltanto** `processing_failed` (il run è fallito, il digest non è arrivato) e
`agent_crashed` (il processo è morto e resta giù finché non interviene qualcuno). Tutto il
resto è `info`. `bot_error` è **`warning`**, non `error`: `on_error` è anche il sink dei
fallimenti del long polling (`NetworkError`, 409 Conflict, `RetryAfter`, con `update=None`),
da cui python-telegram-bot si riprende da solo — classificarlo `error` accende un rosso per
24 h in dashboard su un guasto che non esiste più.

**`operation` per `log_usage`:** `conversation_triage` (l'unica chiamata LLM del run).
Provider `anthropic`, `cost_usd` **non** passato: lo calcola la pricing table.

**Metadata — solo questi:** `job_id`, `window_hours`, `n_conversations`, `n_triaged`,
`duration_ms`, `delivered`, `reason`, `exception`. **Mai** nomi di clienti, `contact_id`,
numeri di telefono, testo dei messaggi, testo del digest, prompt o risposte del modello.
Sui fallimenti viaggiano il **nome della classe** dell'eccezione e un `reason` snake_case
fisso (`callbell_error`, `triage_error`, `unexpected_error`, `delivery_failed`), mai il
messaggio: quello resta su journald.

**Frequenza.** 4 eventi + 1 riga usage per `/triage`, che è manuale. Non aggiungere eventi
nei punti caldi: `_window_messages` (per messaggio), `CallbellClient._get`/`_paginate`
(per pagina HTTP), i loop per-entry dei renderer. E non aggiungere un handler catch-all al
bot per intercettare il polling: romperebbe il silenzio verso gli utenti non autorizzati.

## Fatti Callbell verificati sul dato reale (2026-07-16)
1. Paginazione: envelope `meta: {page, pages}` — iterare finché `page < pages` (NON `data["pagination"]["nextPage"]`).
2. Marcatura in/out: campo messaggio `status` — `received`=cliente, `sent`=operatore, `note`=nota (NON confronto `from`/telefono).
3. Telefono del contatto = `phoneNumber`; `assignedUser` (email o null) = segnale PRESIDIO nel formato neutro.
4. Note di sistema (`status` note, senza `uuid`, `from == to`) distinte dalle note scritte dalle colleghe.
5. `/contacts` ~332 pagine, ordinato per attività: la finestra temporale è il filtro primario, non paginare tutto.
Vedi `docs/dev_notes.md` per il dettaglio.

## Fatti Callbell sulla SCRITTURA, verificati sul dato reale (2026-08-01)
1. `PATCH /contacts/:uuid` con `{"tags": [...]}` è **REPLACE**: la lista è un insieme assoluto, non un delta. Per rimuovere si rimandano tutti gli altri tag.
2. `{"tags": []}` viene salvato davvero: nessun no-op silenzioso sulla lista vuota.
3. Un body PATCH parziale **non** azzera i collaterali (`name`, `note`, `assignedUser`, `customFields`).
4. I nomi dei tag sopravvivono **byte per byte, spazio finale incluso**.
5. **`GET /contacts/:uuid` → `{"contact": {...}}`, un OGGETTO — la doc dice array di un elemento ed è sbagliata** (`json["contact"][0]` → `KeyError`).
6. Il filtro `?tags[]=` è case-insensitive: serve a trovare i candidati, mai a stabilire cosa un contatto porti. Ricontrollo esatto lato client, senza `strip()` né `lower()`.
7. `CallbellClient` è read-only salvo `allow_writes=True`, che `build_adapter()` non passa: il bot **non può** scrivere. Unica scrittura esposta: `update_contact_tags()`.
Vedi `docs/dev_notes.md` per il dettaglio.
