# dev_notes.md — VetTriage v0 (Telegram)

## Convenzioni
- Codice e nomi variabili in INGLESE. Stringhe rivolte all'utente in ITALIANO.
- Python 3.12+. Type hints dove sensato. requests per HTTP.
- Segreti SOLO da variabili d'ambiente. Mai hardcoded, mai committati.
- Un modulo per responsabilità: source_adapter (interfaccia) / callbell_adapter /
  triage_engine / memory / renderers / tts / telegram_bot / storage.

## Principio architetturale n.1: disaccoppiamento dalla fonte
Il triage NON conosce Callbell. Esiste un'interfaccia "source adapter" che restituisce
conversazioni in un formato NEUTRO:
  Conversation = { contact_id (stabile), name, channel, tags[], assigned_user, messages[] }
  Message = { role: CLIENTE|OPERATORE|NOTA_INTERNA|NOTA_SISTEMA, text, timestamp }
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

## Vincoli Callbell (VERIFICATO su dato reale, 2026-07-16)
- Base URL https://api.callbell.eu/v1 — header "Authorization: Bearer <key>".
- API solo su piano "Chat Management Plus" (l'utente RESTA su questo piano; downgrade
  incompatibile col progetto perché toglie l'accesso API).
- Envelope + paginazione: GET /contacts → {contacts[], meta: {page, pages}}; GET
  /contacts/:uuid/messages → {messages[], meta: {page, pages}}. Iterare finché page < pages
  (NON esiste data["pagination"]["nextPage"]).
- Messaggi in ordine createdAt DESCENDENTE. Campo del testo = "text".
- Marcatura IN/OUT: campo messaggio "status" — "received" = CLIENTE, "sent" = OPERATORE,
  "note" = nota. (NON serve confrontare "from" col telefono del contatto.)
- Note: due tipi di status "note" da DISTINGUERE —
  - nota UMANA di una collega: ha "uuid" e "from" != "to" → ruolo NOTA_INTERNA.
  - nota di SISTEMA (es. "Conversation was assigned to X"): NIENTE "uuid" e "from" == "to" → ruolo NOTA_SISTEMA.
- Contatto "assignedUser": email dell'operatore assegnato (oppure null) → segnale per il PRESIDIO,
  entra nel formato neutro come assigned_user.
- Telefono del contatto: campo "phoneNumber" (non "phone"); non serve per l'in/out.
- Volume e ordine: /contacts ha ~332 pagine di storico ed è ordinato per ATTIVITÀ RECENTE (il
  messaggio più recente decresce scendendo nella lista). NON c'è sort server-side ("?sort"
  ignorato) e l'unico timestamp sul contatto è "createdAt" (creazione, NON ultima attività).
  => la FINESTRA TEMPORALE è il filtro primario: paginare /contacts e, per ogni contatto,
  guardare il messaggio più recente; fermarsi dopo N contatti consecutivi fuori finestra.
  MAI paginare tutte le 332 pagine.
- Rate limit: gestire 429 con Retry-After + backoff, pausa ~0.3s tra richieste.
- Campi confermati — Contatto: uuid, name, phoneNumber, createdAt, closedAt, tags[],
  assignedUser, source, channel{uuid,title,type}, note. Messaggio: text, status, uuid, from,
  to, createdAt, channel.

## Le due viste di un contatto (VERIFICATO su dato reale, 2026-08-15)
Il formato neutro legge `name` e `tags` DALLA LISTA (`iter_contacts()` → `_build_conversation`),
e su quel `tags` passa il gate di ogni aggiunta di tag T10 («il contatto ha già `Ricoverato`?»).
Un triage aveva mostrato un nome che la `GET /contacts/:uuid` dello stesso uuid scriveva
diverso: se le due viste divergono sul nome possono divergere sui tag, e allora T10 decide
sulla vista sbagliata. Accertato con scripts/probe_contact_view.py su UN contatto — proprio
quello che aveva dato il sospetto, che è il caso che conta.
- **Le due viste coincidono su TUTTI i campi**, `name` e `tags` inclusi: `differing_fields()`
  è tornato vuoto. Il confronto è sull'unione delle chiavi, quindi un campo che una vista
  omette conterebbe come divergenza — e non ce n'è.
- **Il nome discordante era una rinomina avvenuta fra le due letture**, non una divergenza
  strutturale fra le viste. La lista non è una copia stantia con un suo ciclo di aggiornamento.
- Corollario che sblocca PR3: **`convo.tags` letto dalla lista è una base affidabile** per il
  gate delle aggiunte. Niente `GET /contacts/:uuid` per contatto: una chiamata a contatto
  contro una pagina intera per richiesta, pagata per una freschezza che non risulta esistere.
- Cosa NON è escluso: il tempo che passa fra la lettura della lista e la scrittura alla
  conferma del tap (minuti o ore, in PR3). Quella è freschezza al momento della scrittura ed è
  una domanda diversa: qui cade il difetto STRUTTURALE, non la deriva TEMPORALE.
  **PR3 l'ha chiusa così: si rilegge il contatto con `get_contact()` immediatamente prima di
  ogni PATCH sui tag, e mai dalla lista del triage.** Il gate della PROPOSTA resta sulla
  lista (è lì che si decide se vale la pena chiedere); la LISTA CHE SI SCRIVE nasce sempre da
  una rilettura, perché la PATCH è un REPLACE e una lista vecchia di ore cancellerebbe in
  silenzio i tag messi da una collega nel frattempo. Una GET a tap, e i tap sono rari.
  L'eco della PATCH si confronta **tag per tag e byte per byte, ma ORDINE ESCLUSO**: `tags`
  è un insieme assoluto anche nel modello di Callbell e nessuno legge una posizione, mentre
  far fallire una scrittura riuscita lascerebbe in chat un «⚠️ errore» su un tag che è
  davvero sul contatto. Quello che resta verificato esattamente è ciò che conta: nessun tag
  perso, nessuno inventato, nessun nome normalizzato. Il confronto è fra **multiset**
  (`sorted`, non `set`): un set collasserebbe i duplicati e «nessun tag perso» direbbe il
  falso. E se l'ordine cambia si logga un WARNING col `contact_id`: che Callbell riordini
  non è mai stato verificato (la pulizia del 2026-08-04 toglieva soltanto), così ogni
  scrittura vera diventa un probe su quell'assunzione invece di accettarla in silenzio.
- Portata: un contatto, un momento. Un secondo caso discordante non smentirebbe questo fatto,
  ma varrebbe un altro giro di probe prima di trattare la coincidenza come regola generale.

## Scrittura su Callbell (VERIFICATO su dato reale, 2026-08-01 e 2026-08-05)
Accertato con probe manuali su contatti veri prima di scrivere una riga di codice, perché
la scrittura è distruttiva e irreversibile. Vale per T10, non solo per la pulizia una tantum.
I fatti sui tag sono del 2026-08-01; quelli sul campo name del 2026-08-05 (T10/PR0, probe
scripts/probe_rename.py, nove verifiche tutte passate).
- PATCH /contacts/:uuid con {"tags": [...]} ha semantica **REPLACE**: la lista è un insieme
  ASSOLUTO, non un delta. Per rimuovere un tag si rimandano indietro tutti gli altri.
  Corollario utile: il rinvio dopo una 429 è idempotente.
- **{"tags": []} viene salvato davvero** — eco [] e rilettura []. Nessun no-op silenzioso su
  lista vuota (il rischio classico su stack Rails, dove un array vuoto può arrivare al
  controller come "parametro assente"). È il caso maggioritario, non un caso limite.
- Un body PATCH **parziale non azzera i collaterali**: name, note, assignedUser, customFields
  restano identici prima e dopo. Il campo che farebbe più male è "note", prosa delle colleghe.
- I nomi dei tag sono preservati **byte per byte, spazio finale incluso**: riscrivendo
  "Tommaso rispondi! " torna indietro con lo spazio. Quindi un backup dei tag precedenti è un
  undo eseguibile, non un verbale.
- **ENVELOPE: GET /contacts/:uuid restituisce {"contact": {...}} — un OGGETTO.** La doc
  ufficiale dichiara un array di un elemento ed è SBAGLIATA: json["contact"][0] solleva
  KeyError. Stessa forma nell'eco della PATCH. Nel codice l'unwrap sta in _unwrap_contact(),
  che sull'envelope inatteso solleva CallbellError invece di indovinare.
- Il filtro ?tags[]= è **case-insensitive**: serve a TROVARE i candidati, mai a stabilire
  cosa un contatto porti davvero. Ricontrollo esatto lato client obbligatorio, senza strip()
  né lower().
- **Il campo name si scrive davvero, e sopravvive byte per byte**: "Probe Àèìòù  T10 " è
  tornato identico nell'eco della PATCH e nella rilettura — accenti, doppio spazio interno e
  spazio finale intatti, nessuna normalizzazione lato server. Non era scontato: fino a qui
  era accertato solo che un PATCH parziale non AZZERASSE name, non che scriverlo funzionasse.
- Scrivere name **non tocca i collaterali**, verificato in tutti e quattro gli stadi del probe
  (eco, rilettura, ripristino, rilettura finale): tags, note, assignedUser, customFields,
  phoneNumber identici prima e dopo. Stessa clemenza già nota per il PATCH dei tag, ora
  accertata anche sull'altro verso.
- L'eco della PATCH di name ha la STESSA forma {"contact": {...}} della GET: _unwrap_contact()
  vale per entrambe, non serve un secondo unwrap.
- Il ripristino ha rimesso il nome precedente esatto — " Cognome", **spazio INIZIALE incluso**
  (il nome vero resta fuori da qui: dev_notes sta su GitHub). Corollario: la riga di backup
  fsync'd prima della PATCH è un undo eseguibile anche per il nome, non solo per i tag.
- Garanzia strutturale nel codice: CallbellClient è read-only salvo allow_writes=True.
  build_adapter() non lo passa, quindi il bot Telegram NON PUÒ scrivere — non è che non
  dovrebbe. Le sole due scritture esposte sono update_contact_tags() e update_contact_name():
  niente patch() generico, niente delete, niente invio messaggi, niente assegnazioni.

## Censimento dei tag stantii (VERIFICATO su dato reale, 2026-08-04)
- I tag con contatti sono quattro, ed è la lista TARGET_TAGS di cleanup_stale_tags.py:
  "Ricoverato" (~50), "Risolto" (19), "Noemi rispond!" (11, senza la i — si scrive così),
  "dare Appuntamento" (9). "Tommaso rispondi! " è già stato ripulito.
- Zero contatti, quindi fuori dalla lista: Contattare Urgente, Emergenza, Inviare Fattura,
  Michela rispondi!, Stiamo Arrivando.
- **Un contatto può portarne più d'uno** (visto: ['Ricoverato', 'Risolto']). Quindi lo script
  raccoglie i candidati di tutti i tag, deduplica per contact_id e fa **una sola PATCH per
  contatto**: ciclare tag per tag scrivendo strada facendo raddoppierebbe le finestre di
  rischio sullo stesso contatto. Toglie solo i tag visti in discovery: uno aggiunto da una
  collega nel frattempo non è mai stato misurato sulla soglia, quindi non si tocca.

## T10 — i tag di sistema e l'invariante di `system_tags`
- Il set chiuso gestito dal sistema è `Ricoverato` / `Dimissione oggi` / `Da gestire subito`,
  scritti esattamente così: li leggono le colleghe nella UI di Callbell, non sono
  identificatori interni. Mai `strip()`, mai `lower()`, mai confronto case-insensitive coi
  tag di un contatto (stessa disciplina di TARGET_TAGS).
- **Un tag è "nostro" solo se esiste la riga in `msg_triage.system_tags`, MAI per nome.**
  `Ricoverato` è byte-identico a quello che le colleghe usano a mano (~50 contatti al
  censimento del 2026-08-04): il nome non distingue niente. La migration 0002 lo mette nero
  su bianco col vincolo `unique (contact_id, tag)` — `system_tags` è **lo stato corrente**
  dei tag che abbiamo messo noi, non uno storico: la storia sta in `proposals`, riga per riga
  col suo motivo e il suo esito.
- Conseguenza ACCETTATA, non ignorata: un `Ricoverato` messo a mano da una collega non viene
  mai rimosso dalla regola semantica. Ci arriva solo la rete anti-fossile dei 14 giorni.
- **Un tag non si rimuove MAI perché è passato del tempo.** Una degenza lunga con la chat
  silente deve tenere il suo tag: per questo `fatti.ricovero` ha tre valori e non è un
  booleano — `non_menzionato` non è "dimesso", è "in questa finestra non se n'è parlato".
- Le rimozioni a calendario (`Dimissione oggi` il giorno dopo, `Da gestire subito` a 48 h)
  nascono **alla conferma dell'aggiunta**, non nello stesso run che propone l'aggiunta: una
  rimozione programmata di un tag che potrebbe non essere mai applicato sarebbe una riga da
  interpretare, senza uno stato onesto da darle se la proposta viene ignorata.
- Tutte le maturazioni a calendario cadono alle **07:00 Europe/Rome** del giorno dopo, da un
  unico helper. Mezzanotte farebbe maturare nel cuore della notte una cosa che si guarda la
  mattina, e due convenzioni orarie per due regole gemelle si pagano mesi dopo.
- «Oggi» è sempre quello di Roma, mai quello di UTC: è la stessa data che il blocco fatti dà
  al modello, e `Dimissione oggi` è precisamente una regola same-day.
- Tutto ciò che arriva dal modello ed entra in un'aritmetica (una data di maturazione) o in
  una stringa scritta su un record vero (un nome contatto) passa prima da un controllo di
  plausibilità. Un valore che non riconosciamo produce NESSUNA proposta, mai una sbagliata.

### La consegna e il tap (PR3)
- **La coda non è del run.** Si consegna ogni proposta `pending` e matura (`matures_at` nullo
  o passato), ordinata per `created_at`, anche a finestra vuota: una rimozione maturata alle
  07:00 non è affare della finestra che si sta guardando. PR4 aggiunge il *job*, non un'altra
  logica di consegna.
- **Una riga si consegna una volta sola**: il filtro è `telegram_message_id is null` e la
  colonna si scrive DOPO l'invio. Al contrario, un invio fallito seppellirebbe la domanda per
  sempre. Un doppione (invio riuscito, registrazione fallita) costa un messaggio in più: il
  claim rende inerte il secondo tap.
- **La difesa dal doppio tap vive nel DB**, che è l'unico posto dove due tap si incontrano:
  la PATCH di claim porta `stato=eq.pending` e `return=representation`. Zero righe indietro
  = qualcuno ha già deciso. `decided_at` si scrive lì, ed è da lì che si misurano i 30 giorni
  di riproponibilità di un tag rifiutato.
- **`answer()` vale una volta sola per query**, quindi il controllo del flag viene PRIMA: un
  `answer()` vuoto renderebbe invisibile il toast «Proposte disattivate». E a flag spento non
  si edita il messaggio: la tastiera resta, la riga resta `pending`, il kill switch si
  riaccende senza toccare il DB.
- **Un tap non autorizzato non riceve nemmeno un `answer()`.** `CallbackQueryHandler` non
  accetta `filters`, quindi la whitelist è dentro il handler, con lo stesso silenzio dei
  comandi: rispondere confermerebbe che il bot esiste.
- **Guasto del DB dopo una PATCH riuscita ≠ errore.** Il tag È sul contatto: dire «errore»
  manderebbe a cercare una scrittura che c'è. L'esecutore torna un esito che racconta cosa è
  successo e cosa resta da sistemare a mano. Risalgono solo i guasti *prima* della scrittura.
- **Una riga che il codice non sa leggere** (tipo sconosciuto, payload rotto) si salta in
  consegna e, se il claim l'ha già presa, si chiude `fallita`: lasciata `approvata` resterebbe
  lì per sempre a sembrare lavoro in corso. Un solo parser (`row_to_stored`) per i due momenti.

## Anti-pattern (NON fare)
- NON usare webhook in v0. Pull a comando.
- NON esporre chiavi lato client. Tutto sul backend Hetzner.
- NON renderizzare tabelle ricche su Telegram (monospace fragile su mobile). Testo semplice.
  La tabella "vera" è feature del v1 con la PWA.
- Emoji DECORATIVE no; INDICATORI SEMANTICI di stato sì. I pallini urgenza (🔴🟠🟡⚪),
  presidio (❗/✅) e temperatura (🔥/⚠️) in schema e tabella comunicano lo stato a colpo
  d'occhio, non decorano. Il vocale resta pulito (nessun simbolo, nessun tag).
- NON fidarsi di tag/note come unica verità (uso irregolare).
- NON far rispondere il bot a chiunque: whitelist obbligatoria sull'ID Telegram.
- NON mandare un blocco unico: schema, tabella, vocale = tre messaggi distinti.
- NON far vedere al triage_engine strutture dati Callbell-specifiche (passa dal formato neutro).
- NON essere zelanti sulle promesse scadute (vedi memoria).
- NON costruire la lista di tag da scrivere partendo da `convo.tags` del triage: la PATCH è un
  REPLACE e quella lista è vecchia. Sempre `get_contact()` prima, eco verificata dopo.
- NON dare `allow_writes` a `build_adapter()`/`build_read_client()` «perché ora serve»: la
  porta è `build_write_client()`, e la apre solo l'esecutore dopo un tap confermato.

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
