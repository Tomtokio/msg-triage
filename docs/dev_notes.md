# dev_notes.md — VetTriage v0 (Telegram)

## Convenzioni
- Codice e nomi variabili in INGLESE. Stringhe rivolte all'utente in ITALIANO.
- Python 3.11+. Type hints dove sensato. requests per HTTP.
- Segreti SOLO da variabili d'ambiente. Mai hardcoded, mai committati.
- Un modulo per responsabilità: source_adapter (interfaccia) / callbell_adapter /
  triage_engine / memory / renderers / tts / telegram_bot / storage.

## Principio architetturale n.1: disaccoppiamento dalla fonte
Il triage NON conosce Callbell. Esiste un'interfaccia "source adapter" che restituisce
conversazioni in un formato NEUTRO:
  Conversation = { contact_id (stabile), name, channel, tags[], messages[] }
  Message = { role: CLIENTE|OPERATORE|NOTA_INTERNA, text, timestamp }
callbell_adapter è UNA implementazione. Domani un whatsapp_adapter o altro BSP si aggiunge
senza toccare triage_engine, memory, renderers. Questo è ciò che rende il triage un asset
portabile anche quando l'utente lascerà Callbell.

## Principio architetturale n.2: un solo triage strutturato, due profondità di resa
triage_engine ritorna UN oggetto JSON strutturato. Da lì i renderers producono:
- vocale = sintetico (allarme)
- schema/tabella = giornale di bordo completo
NON fare tre chiamate a Claude. Una sola, poi si rende diversamente. Garantisce coerenza.

## Il doppio ruolo (allarme + giornale di bordo)
Il livello "in corso" NON è "5 gestite, nessuna azione". È una rassegna narrativa con una
micro-storia per conversazione. L'utente vuole consapevolezza di TUTTO, non solo delle eccezioni.

## Dettaglio proporzionale alla temperatura
Istruire ESPLICITAMENTE il prompt: allocare parole in base a quanto la conversazione è calda/
delicata. Routine → mezza riga. Calda o clinicamente delicata → due-tre righe. La tendenza
naturale del modello è uniformare: va contrastata nel prompt.

## Paletto etico (NON negoziabile)
Il prompt descrive lo STATO DELLE CONVERSAZIONI, mai giudica l'OPERATO delle colleghe.
- SÌ: "la sig.ra Rossi aspetta ancora risposta".
- NO: "Giulia è in ritardo".
Consapevolezza, non sorveglianza. Se le colleghe percepissero lo strumento come controllo sul
loro lavoro, cambierebbe il clima. Inserire questo vincolo nero su bianco nel system prompt.

## Memoria: due livelli, priorità alla prudenza
- BASE (affidabile): confronto di stato tra run via contact_id. Delta: nuova / ancora scoperta /
  aspetta da N run / cambiata. Fa il 70% del valore col 30% della complessità. Nessuna
  interpretazione fragile.
- RAFFINATO (sperimentale nel v0): promesse scadute. Rilevazione IMPERFETTA per natura (si deduce
  una scadenza implicita dal linguaggio). Trattare come INDIZIO, non dato certo.
  ANTI-PATTERN da evitare: falsi allarmi da promessa. Se il modello vede impegni-con-scadenza
  ovunque, riempie il triage di "scaduto!" falsi e perde la fiducia dell'utente. REGOLA:
  segnalare scaduto SOLO se (a) la promessa era esplicita e (b) il tempo è chiaramente passato e
  (c) non c'è una risposta successiva visibile. Nel dubbio, tacere.

## Due concetti di tempo nella memoria
- last_message_at: quando è stato visto l'ultimo messaggio della conversazione.
- promessa_scadenza_stimata: quando era attesa una risposta (solo se una promessa esplicita è
  stata rilevata). Da questi due nasce il segnale "promessa scaduta". Sono campi distinti.

## Vincoli Callbell (da documentazione, DA CONFERMARE su dato reale)
- Base URL https://api.callbell.eu/v1 — header "Authorization: Bearer <key>".
- API solo su piano "Chat Management Plus" (l'utente RESTA su questo piano; downgrade
  incompatibile col progetto perché toglie l'accesso API).
- GET /contacts/:uuid/messages → messaggi in ordine createdAt DESCENDENTE, paginati.
- Note interne: status == "note".
- Rate limit: gestire 429 con Retry-After + backoff, pausa ~0.3s tra richieste.
- DA VERIFICARE AL PRIMO LANCIO:
  1. Schema esatto paginazione (ipotizzato data["pagination"]["nextPage"]). Fare una curl reale
     a /contacts?page=1 e adattare.
  2. Marcatura messaggio IN ENTRATA vs USCITA. Approccio attuale: confronto campo "from" col
     telefono del contatto. Validare sul dato vero PRIMA di fidarsi.

## Anti-pattern (NON fare)
- NON usare webhook in v0. Pull a comando.
- NON esporre chiavi lato client. Tutto sul backend Hetzner.
- NON renderizzare tabelle ricche su Telegram (monospace fragile su mobile). Testo semplice.
  La tabella "vera" è feature del v1 con la PWA.
- NON fidarsi di tag/note come unica verità (uso irregolare).
- NON far rispondere il bot a chiunque: whitelist obbligatoria sull'ID Telegram.
- NON mandare un blocco unico: schema, tabella, vocale = tre messaggi distinti.
- NON far vedere al triage_engine strutture dati Callbell-specifiche (passa dal formato neutro).
- NON essere zelanti sulle promesse scadute (vedi memoria).

## Dipendenza aperta da risolvere (T6)
TTS per il vocale. Verificare se Leggo AI (PWA TTS esistente dell'utente) espone un endpoint
richiamabile server-side (ispezione codice con Claude Code: cercare tts/speech/synthesize/
elevenlabs/speechSynthesis). Tre scenari: backend proprio (endpoint riusabile) / servizio
esterno con chiave (riusare la chiave) / speechSynthesis nel browser (NON richiamabile da
server, serve TTS nostro). Non bloccare il resto: T8 gira con stub audio mentre si decide.

## Privacy / GDPR
Messaggi con dati clinici e proprietari identificabili.
- Niente log persistente del contenuto in chiaro oltre il necessario.
- Supabase: la tabella conversation_states contiene nomi reali (servono all'utente per agire).
  Proteggere con RLS e accesso ristretto.
- Pipeline di pseudonimizzazione dell'utente (Presidio+GLiNER+LLM judge) disponibile come
  opzione agganciabile; NON obbligatoria per l'uso live del v0. Diventa obbligatoria SE/QUANDO
  i dati vengono usati per addestrare un bot (progetto separato).

## Riuso da prototipi esistenti
callbell_export.py (logica fetch/paginazione) e callbell_triage.py (fetch finestra temporale +
TRIAGE_SYSTEM prompt base sul dominio esotici/aviari). Il prompt va ESTESO con: doppio ruolo,
dettaglio per temperatura, paletto etico. Riusare come punto di partenza, non copiare tale quale.
