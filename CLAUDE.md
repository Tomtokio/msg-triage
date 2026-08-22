# CLAUDE.md — msg-triage

## Cos'è questo progetto
Triage intelligente delle conversazioni WhatsApp della clinica (via Callbell).
Legge i messaggi recenti, li giudica, produce un digest a tre livelli.
**Non risponde mai ai clienti.** Il triage è sola lettura; le uniche scritture che il
progetto si concede sono sui *tag* e sul *nome* dei contatti, e solo dopo una conferma
esplicita su Telegram (T10). Mai un messaggio, mai niente che il cliente possa vedere.

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

## T10 — tag di sistema e proposte (dietro `ENABLE_PROPOSALS`, default OFF)
- Set chiuso, scritto **esattamente così** perché lo leggono le colleghe nella UI di
  Callbell: `Ricoverato` / `Dimissione oggi` / `Da gestire subito`. Mai `strip()`, mai
  `lower()`, confronto byte per byte. (`docs/prompts/prompt-t10-proposte.md` riporta ancora
  i nomi vecchi `ricoverato / dimissione-oggi / triage-urgente`: sono superati.)
- **Un tag è "nostro" solo se esiste la riga in `msg_triage.system_tags`, mai per nome.**
  `Ricoverato` è byte-identico a quello che le colleghe usano a mano: il nome non distingue
  niente. `system_tags` è lo stato corrente, non uno storico — la storia sta in `proposals`.
- **Il modello non decide e non scrive mai.** Estrae i fatti; le regole deterministiche
  (`msg_triage/proposals.py` — pure: niente rete, niente config, `now` iniettato) li
  traducono in proposte tipizzate.
- **Nessuna proposta esiste se non è persistita**: l'idempotenza vive nel DB. Perciò
  `proposal_store.py` NON è fail-silent come `storage.save_triage_run`: i suoi errori si
  propagano. E il flag da solo non basta — senza un Supabase vero non si producono proposte.
- Un tag non si rimuove **mai** perché è passato del tempo. Le maturazioni a calendario
  cadono alle 07:00 Europe/Rome; «oggi» è sempre quello di Roma, mai quello di UTC.
- **Stato: PR3.** Le proposte arrivano su Telegram dopo i tre messaggi, una per messaggio,
  coi bottoni ✅/❌; solo il tap ✅ scrive su Callbell. Il *claim* è un compare-and-swap sul
  DB (`PATCH …&stato=eq.pending` con `return=representation`): la difesa dal doppio tap sta
  lì, non in memoria. Si consegna **tutta la coda matura**, non solo le proposte di questo
  run — anche a finestra vuota, perché una rimozione maturata alle 07:00 non è affare di
  questa finestra. Una riga si consegna **una volta sola** (`telegram_message_id`), e si
  registra dopo l'invio, mai prima.
- **Prima di ogni PATCH sui tag si rilegge il contatto** (`get_contact`). La lista letta dal
  triage è vecchia di ore quando arriva il tap, e la PATCH è un REPLACE: scriverla
  cancellerebbe in silenzio i tag messi dalle colleghe nel frattempo. L'eco si confronta
  **tag per tag, byte per byte, ordine escluso** (`tags` è un insieme anche per Callbell:
  far fallire una scrittura riuscita per un riordino sarebbe l'errore peggiore dei due).
  Sulla rinomina non serve rileggere (nessun collaterale da perdere), l'eco si verifica lo
  stesso.
- **Un guasto del DB *dopo* una PATCH riuscita non è «errore»**: il tag è sul contatto, e il
  messaggio in chat dice esattamente quello e cosa resta da sistemare a mano. Risalgono solo
  i guasti *prima* della scrittura, che diventano `fallita`.
- A flag spento un tap risponde solo «Proposte disattivate» e **non edita il messaggio**: la
  tastiera resta e la riga resta `pending`, così il kill switch si riaccende senza toccare
  il DB.
- **Niente eventi di telemetria per le proposte.** L'audit trail è la riga `proposals`
  (`created_at` → `telegram_message_id` → `decided_at` → `executed_at` → `stato`); il
  vocabolario chiuso dei metadata non ha un campo per una proposta, e un errore si vede in
  chat subito.

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

## Fatti Callbell verificati sul dato reale (2026-07-16 e 2026-08-15)
1. Paginazione: envelope `meta: {page, pages}` — iterare finché `page < pages` (NON `data["pagination"]["nextPage"]`).
2. Marcatura in/out: campo messaggio `status` — `received`=cliente, `sent`=operatore, `note`=nota (NON confronto `from`/telefono).
3. Telefono del contatto = `phoneNumber`; `assignedUser` (email o null) = segnale PRESIDIO nel formato neutro.
4. Note di sistema (`status` note, senza `uuid`, `from == to`) distinte dalle note scritte dalle colleghe.
5. `/contacts` ~332 pagine, ordinato per attività: la finestra temporale è il filtro primario, non paginare tutto.
6. **Lista `/contacts` e `GET /contacts/:uuid` coincidono** (`name`, `tags`, tutti i campi): un nome discordante è una rinomina fra le due letture, non una divergenza fra le viste. Quindi `convo.tags` dalla lista è base affidabile per il gate T10. Verificato 2026-08-15 con `scripts/probe_contact_view.py`, su un contatto.
Vedi `docs/dev_notes.md` per il dettaglio.

## Fatti Callbell sulla SCRITTURA, verificati sul dato reale (2026-08-01 e 2026-08-05)
1. `PATCH /contacts/:uuid` con `{"tags": [...]}` è **REPLACE**: la lista è un insieme assoluto, non un delta. Per rimuovere si rimandano tutti gli altri tag.
2. `{"tags": []}` viene salvato davvero: nessun no-op silenzioso sulla lista vuota.
3. Un body PATCH parziale **non** azzera i collaterali (`name`, `note`, `assignedUser`, `customFields`).
4. I nomi dei tag sopravvivono **byte per byte, spazio finale incluso**.
5. **`GET /contacts/:uuid` → `{"contact": {...}}`, un OGGETTO — la doc dice array di un elemento ed è sbagliata** (`json["contact"][0]` → `KeyError`).
6. Il filtro `?tags[]=` è case-insensitive: serve a trovare i candidati, mai a stabilire cosa un contatto porti. Ricontrollo esatto lato client, senza `strip()` né `lower()`.
7. `CallbellClient` è read-only salvo `allow_writes=True`. `build_adapter()` e `build_read_client()` non lo passano: il path di lettura **non può** scrivere. L'unica porta è `build_write_client()`, e la apre solo `msg_triage/proposal_executor.py` dopo un tap ✅ su una proposta che esiste sul DB. Le sole due scritture esposte restano `update_contact_tags()` e `update_contact_name()`.
8. **Anche `name` si scrive davvero e sopravvive byte per byte** (accenti, doppio spazio, spazio finale), senza toccare i collaterali e con la stessa forma di envelope nell'eco. Verificato 2026-08-05 con `scripts/probe_rename.py`.
Vedi `docs/dev_notes.md` per il dettaglio.
