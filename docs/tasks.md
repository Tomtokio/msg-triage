# tasks.md — VetTriage v0 (Telegram)

Granularità media: 1 task = 1 feature coerente. Ordine = dipendenze.

---

## T1 — Setup progetto e segreti
Scaffolding Python sull'Hetzner, configurazione e segreti.
- Struttura cartelle, requirements (requests, python-telegram-bot, anthropic, supabase)
- Variabili d'ambiente: CALLBELL_API_KEY, ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN,
  TELEGRAM_ALLOWED_USER_ID, SUPABASE_URL, SUPABASE_KEY
- Logging base
**Completamento:** il progetto parte, legge i segreti, logga "ready".
**Dipendenze:** nessuna.

## T2 — Source adapter + client Callbell
Recupero conversazioni recenti, dietro un'interfaccia astratta (source adapter).
- Definire l'interfaccia neutra: una conversazione = {contact_id stabile, nome, canale, assigned_user,
  lista messaggi con {role: CLIENTE/OPERATORE/NOTA_INTERNA/NOTA_SISTEMA, testo, timestamp}, tags}
- Implementare l'adattatore Callbell su questa interfaccia:
  GET /contacts (paginato) + GET /contacts/:uuid/messages
- Filtro per finestra temporale (default 6h, parametrico)
- Ruoli: CLIENTE / OPERATORE / NOTA_INTERNA / NOTA_SISTEMA
- Schema paginazione e marcatura messaggi: VERIFICATO su dato reale (2026-07-16 — vedi dev_notes.md)
**Completamento:** funzione che ritorna conversazioni nel formato NEUTRO (non Callbell-specifico).
**Dipendenze:** T1.
**Nota:** il triage a valle non deve mai vedere strutture dati Callbell-specifiche.

## T3 — Motore di triage (Claude) con output strutturato
Da conversazioni neutre → triage strutturato.
- Prompt di sistema (riusare TRIAGE_SYSTEM dal prototipo come base, ma AGGIORNARLO con:
  doppio ruolo allarme+giornale, dettaglio proporzionale alla temperatura, paletto etico)
- Output JSON strutturato: per ogni conversazione {contact_id, nome, gruppo (subito/in_corso/
  rumore), motivo, urgenza, presidio, temperatura, stato_sintetico, azione_suggerita,
  promessa_rilevata (opzionale: {testo, scadenza_stimata})}
- UNA sola chiamata API
**Completamento:** dato un set di conversazioni, ritorna oggetto triage strutturato completo.
**Dipendenze:** T2.

## T4 — Memoria: confronto tra run (Supabase)
Confronta il triage corrente col precedente. DUE livelli.
- LIVELLO BASE (affidabile): per ogni conversazione, recupera lo stato dell'ultimo run salvato
  (via contact_id) e calcola i delta: nuova? / ancora scoperta? / cliente aspetta da N run? /
  cambiata rispetto a prima?
- LIVELLO RAFFINATO (prudente/sperimentale): promesse scadute. Se un run precedente aveva
  rilevato una promessa con scadenza stimata e ora il tempo è passato E la conversazione non
  mostra risposta successiva → segnala "promessa non mantenuta". CONSERVATIVO: solo promesse
  esplicite, solo tempo chiaramente passato. In caso di dubbio, NON segnalare.
- Arricchisce l'oggetto triage con i campi di memoria prima del rendering.
**Completamento:** l'oggetto triage porta i delta di stato e (se presenti) le promesse scadute.
**Dipendenze:** T3, T7 (schema Supabase). Coordinare con T7.

## T5 — Renderer dei tre formati (con memoria)
Da UN triage strutturato+memoria → tre modalità.
- SCHEMA: prosa a tre livelli. Livello "in corso" = rassegna narrativa. Dettaglio PROPORZIONALE
  ALLA TEMPERATURA (routine mezza riga, calde due-tre righe). Integra i segnali di memoria
  ("aspetta ancora da stamattina", "promessa delle 12 non mantenuta"). GIORNALE COMPLETO.
- TABELLA: testo formattato compatto, 1 riga per conversazione, colonne chiave. GIORNALE COMPLETO.
  NO rendering ricco in v0.
- VOCALE (testo): SINTETICO. Solo urgenti + una frase di panoramica ("altre sei in corso, tutte
  presidiate"). Frasi pulite per l'ascolto, no elenchi fitti, apre con l'urgenza.
**Completamento:** tre output pronti dalla consegna.
**Dipendenze:** T4.

## T6 — Generazione audio (TTS)
Converte il testo "vocale" di T5 in file audio (ogg/mp3) per Telegram.
- DECISIONE: verificare se Leggo AI espone endpoint TTS server-side (ispezione codice con
  Claude Code). Se sì → usarlo. Se no → TTS provvisorio in v0 + TODO per Leggo AI in v1.
**Completamento:** dato il testo vocale, ritorna un file audio valido.
**Dipendenze:** T5. Può procedere con uno stub mentre si decide.

## T7 — Persistenza + schema memoria + ciclo di vita proposte (Supabase)
Schema che serve TRE padroni: storico, memoria tra run, e le proposte/tag di T10.
Progettare la migration COMPLETA in una volta (la applica Tommaso a mano).
- Tabella triage_runs: id, created_at, finestra_ore, n_conversazioni, schema_text,
  table_text, vocal_text
- Tabella conversation_states: id, run_id (fk), contact_id (INDICIZZATO), nome, gruppo,
  presidio, urgenza, temperatura, stato_sintetico, specie (nullable — alimenta
  l'autosufficienza delle voci quando la specie è nei messaggi vecchi fuori finestra),
  last_message_at, promessa_testo (nullable), promessa_scadenza_stimata (nullable)
  → è questa tabella che abilita la MEMORIA: al run successivo si interroga per contact_id
- Tabella proposals (per T10): id, created_at, contact_id, tipo (tag_add/tag_remove/
  rename), payload, motivo, stato (pending/approvata/rifiutata/eseguita/fallita),
  matures_at (nullable, per le proposte programmate), executed_at, telegram_message_id
- Tabella system_tags (per T10): contact_id, tag, applied_at, proposta_id
  → il ciclo di vita dei tag richiede di sapere quando e perché un tag è stato messo
- Due concetti di tempo: last_message_at (quando visto l'ultimo msg) e
  promessa_scadenza_stimata (quando era attesa risposta, se rilevata)
- Salvataggio dopo ogni run
- RLS / accesso ristretto (contiene nomi reali)
**Completamento:** ogni run scrive run + stati per conversazione; interrogabile per
contact_id; le tabelle di T10 esistono e sono pronte.
**Dipendenze:** T1. (T4 e T10 dipendono da questo schema.)

## T8 — Bot Telegram (orchestrazione)
Interfaccia di comando e consegna.
- Comando /triage (e /triage 12 per finestra in ore)
- Whitelist: risponde SOLO a TELEGRAM_ALLOWED_USER_ID
- Orchestrazione: T2 → T3 → T7(carica stato precedente) → T4 → T5 → T6 →
  invia schema (testo), tabella (testo), vocale (audio) → T7(salva stato corrente)
- Messaggi di stato e gestione "nessuna conversazione recente"
**Completamento:** da Telegram, /triage restituisce le tre modalità con memoria attiva.
**Dipendenze:** T5 (T6 con stub accettabile per primo giro), T7.

## T9 — Deploy e scheduling opzionale
- Servizio systemd sull'Hetzner per il bot sempre attivo
- (Opzionale) cron per un /triage automatico a orari fissi in push
**Completamento:** il bot gira stabile come servizio.
**Dipendenze:** T8.

## T10 — Azioni organizzative con conferma (proposte: tag + rinomina)
Vedi prompt dedicato (prompt-t10-proposte.md). In sintesi: il triage estrae fatti di
stato dal testo (stessa chiamata LLM); regole deterministiche generano PROPOSTE
(aggiungi/rimuovi tag del set chiuso, rinomina contatto secondo convenzione); ogni
proposta arriva su Telegram con bottoni ✅/❌; solo alla conferma il codice scrive su
Callbell. Set chiuso v1: ricoverato / dimissione-oggi / triage-urgente, ognuno con la
propria regola di ciclo di vita. Tag delle colleghe intoccabili. Controllo di coerenza
al risveglio delle conversazioni dormienti (anti-fossile). Multi-animale → nome = solo
proprietario.
**Completamento:** le proposte arrivano, i tap eseguono, il DB traccia il ciclo completo.
**Dipendenze:** T9 (bot sempre attivo), T7 (tabelle proposals + system_tags).

## Ordine di lavoro aggiornato (post-V0)
PR rumore → T9 (systemd VPS + cron opzionale) → T7 (migration completa) → T10 (proposte)
→ T4 (memoria, si aggancia allo schema già pronto) → T6 (audio via Leggo AI, quando
verificato l'endpoint).
