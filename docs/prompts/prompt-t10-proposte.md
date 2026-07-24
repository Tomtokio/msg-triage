# T10 — Azioni organizzative su Callbell con conferma Telegram (proposte: tag + rinomina)

Nuova feature, progettata in brainstorming con l'utente. Leggi CLAUDE.md e docs/, poi
pianifica. Non scrivere codice finché non approvo. NOTA: questo task dipende da T9
(bot sempre attivo su VPS) e da T7 (schema Supabase, vedi sotto): se non sono ancora
fatti, dillo e fermati — l'ordine è T9 → T7 → T10.

## La visione (diversa dal semplice "tag di allarme")

I tag su Callbell diventano una FOTOGRAFIA DELLO STATO organizzativo di ogni paziente,
mantenuta aggiornata dal sistema: "ricoverato" compare quando l'animale entra in
degenza, sparisce quando esce. In più, il sistema propone la correzione dei NOMI
contatto (le colleghe non li sistemano mai: restano nomi WhatsApp di default,
inutilizzabili).

TUTTO passa per proposta + conferma dell'utente su Telegram (bottoni inline
✅ Applica / ❌ Ignora). Nessuna azione automatica in questa fase: la conferma manuale
è anche uno strumento di calibrazione — dopo settimane di tap sapremo quali regole
automatizzare.

## Principio architetturale NON NEGOZIABILE

Il modello NON scrive mai e NON decide mai le azioni. Catena:
1. Il triage engine (T3), nella STESSA unica chiamata di oggi, estrae anche i FATTI DI
   STATO dal testo (estensione dello schema JSON: es. ricovero_in_corso,
   dimissioni_previste con orario, animali_menzionati {specie, nome}, nome_proprietario
   se si firma). Zero chiamate LLM aggiuntive.
2. REGOLE DETERMINISTICHE (nuovo modulo, es. proposals.py) traducono i fatti in
   PROPOSTE tipizzate: aggiungi tag X / rimuovi tag X / rinomina in Y.
3. Le proposte vanno su Supabase (stato: pending) e vengono consegnate su Telegram
   con bottoni inline.
4. Solo al tap ✅ dell'utente il codice esegue la scrittura su Callbell.
   ❌ marca la proposta rifiutata: MAI riproposta per lo stesso contatto+tipo
   (idempotenza via DB).

## Il set chiuso di tag gestiti (v1 — configurabile, apribile poi)

Il sistema gestisce SOLO i propri tag. I tag applicati dalle colleghe sono INTOCCABILI,
sempre. Ogni tag del set porta la propria regola di ciclo di vita:

| Tag | Applicazione (proposta) | Rimozione (proposta) |
|---|---|---|
| `ricoverato` | il testo indica degenza in corso | SOLO su segnale dal testo (dimissioni avvenute o fissate+24h). MAI a tempo: una degenza lunga con chat silente non deve perdere il tag |
| `dimissione-oggi` | il testo fissa dimissioni in giornata | a calendario: il giorno dopo |
| `triage-urgente` | gruppo = DA GESTIRE SUBITO | chat inattiva/gestita da 48h |

Le regole "a calendario" e "a inattività" richiedono proposte PROGRAMMATE: salvate su
Supabase con timestamp di maturazione, un job periodico le ripesca e le consegna quando
maturano (stesso pattern timestamp-nel-DB del debounce di pdf-analisi-archive: robusto
ai riavvii, mai timer in memoria).

## Controllo di coerenza al risveglio (la rete anti-fossile)

Quando arriva attività su una conversazione ferma da molto (soglia configurabile, es.
14 giorni), il triage — che la sta già leggendo — confronta i tag di sistema presenti
con i fatti estratti ORA: se vede `ricoverato` ma nessun ricovero in corso, propone la
rimozione. Il tag fossile muore quando la chat torna viva, non a un timer cieco.

## Rinomina contatti (stesso pattern proposta+conferma)

Problema: i nomi contatto restano quelli di default WhatsApp (nome proprio, soprannomi,
numeri) — inutilizzabili per collegare proprietario e paziente.

- Convenzione nome (template configurabile): "{Nome Cognome} {specie} {nome animale}"
  → "Mario Rossi coniglio Bunny".
- REGOLA MULTI-ANIMALE (decisa dall'utente): se i fatti indicano PIÙ animali per lo
  stesso contatto, il nome proposto è SOLO il proprietario ("Mario Rossi") — gli
  animali stanno nei tag o nelle note, non nel nome. Corollario: se un contatto già
  rinominato con animale rivela un secondo animale, proporre la SEMPLIFICAZIONE al
  solo proprietario (motivandola: "ha più animali: Bunny, Titti").
- Trigger della proposta: nome attuale palesemente povero (euristica deterministica:
  una sola parola, contiene cifre, ecc.) E fatti sufficienti per fare meglio. Con
  informazione parziale si propone il parziale ("Mario Rossi" anche senza animale).
- MAI dedurre o inventare specie/nomi non presenti nei messaggi (regola già nel prompt
  di triage: vale anche qui).
- Proposta rifiutata → contatto non riproposto (DB).

## UX Telegram delle proposte

- Le proposte arrivano DOPO i tre messaggi di triage, una per messaggio, con bottoni
  inline ✅/❌ (CallbackQueryHandler; il bot ha già la whitelist — i callback vanno
  filtrati allo stesso modo).
- Testo compatto e autosufficiente: "🏷️ Aggiungere tag `ricoverato` a **Gabriele Di
  Resta**? (dal testo: animale in degenza, dimissioni non fissate)" /
  "✏️ Rinominare **'Gabri92'** in **'Gabriele Di Resta parrocchetto Saetta'**?"
- Al tap: esegui → edita il messaggio col risultato ("✅ Fatto" / "⚠️ Errore su
  Callbell: …"). Niente proposte duplicate in coda: se una proposta identica è già
  pending, non crearne un'altra.
- Le scritture su Callbell sono best-effort: un errore si mostra e si logga, non
  blocca mai nulla.

## Vincoli di sicurezza

- Operazioni ammesse: aggiungi tag (set chiuso), rimuovi tag (SOLO del set di sistema),
  rinomina contatto. NIENT'ALTRO: mai riassegnare, mai chiudere, mai messaggi ai clienti.
- Feature flag ENABLE_PROPOSALS (default OFF): a flag spento il triage è identico a oggi.
- Endpoint di scrittura Callbell (tag, update contatto): LEGGERLI dalla doc
  https://docs.callbell.eu — non dedurli. Se la doc non basta, fermati e chiedi
  all'utente la verifica con curl reale (come per paginazione e status in T2).

## Impatto su T7 (schema Supabase) — da progettare INSIEME a questo

La migration di T7 (applicata a mano dall'utente, come da CLAUDE.md) deve prevedere
fin dall'inizio, oltre a triage_runs e conversation_states (con campo `specie`):
- tabella `proposals`: id, created_at, contact_id, tipo (tag_add/tag_remove/rename),
  payload (tag o nuovo nome), motivo, stato (pending/approvata/rifiutata/eseguita/
  fallita), matures_at (nullable, per le programmate), executed_at, telegram_message_id
- tabella (o campi) per i tag di sistema applicati: contact_id, tag, applied_at,
  proposta_id — serve al ciclo di vita (sapere QUANDO e PERCHÉ un tag è stato messo)

## Test attesi (indicativi)

- Regole: fatti → proposte corrette per ciascun tag del set; multi-animale → nome solo
  proprietario; nome già conforme → nessuna proposta
- Idempotenza: proposta identica pending → non duplicata; rifiutata → non riproposta
- Ciclo di vita: dimissione fissata → proposta programmata con matures_at corretto;
  `ricoverato` MAI rimosso a tempo
- Flag OFF → zero proposte, triage identico
- Callback Telegram: ✅ esegue e aggiorna stato; ❌ marca rifiutata; utente non
  whitelistato → ignorato
- Errore Callbell in esecuzione → stato "fallita", messaggio editato, niente crash

## Fuori scope esplicito

- Qualsiasi automatismo senza conferma (verrà DOPO la calibrazione, regola per regola)
- Tag delle colleghe: mai toccati, nemmeno in proposta
- Risposte ai clienti
- Tag oltre il set chiuso (l'apertura del set è una evoluzione successiva)
