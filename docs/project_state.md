# project_state.md — VetTriage v0 (Telegram)

## Cos'è
Strumento personale di triage WhatsApp per il responsabile del reparto esotici/aviari.
Quando non è in clinica, vuole sapere a colpo d'occhio cosa sta succedendo nelle chat
WhatsApp gestite (in modo irregolare) dalle colleghe via Callbell.

NON risponde ai messaggi. Legge soltanto e produce un triage intelligente.

## Il doppio ruolo del triage (IMPORTANTE)
Il triage ha DUE funzioni, non una:
1. ALLARME — dove serve l'intervento personale del responsabile.
2. GIORNALE DI BORDO — cosa sta succedendo in TUTTE le conversazioni aperte, anche quelle
   che non richiedono nulla da lui. Il responsabile vuole consapevolezza di fondo, non solo
   le eccezioni. Esempio: "la sig.ra Bianchi ha chiesto se il coniglio può essere dimesso,
   le colleghe le hanno detto che entro due ore arriva conferma per stasera."

Di conseguenza il livello "in corso" NON è un contenitore muto ("5 gestite"), ma una
RASSEGNA NARRATIVA: una micro-storia per conversazione (chi ha chiesto cosa, a che punto è).

## Obiettivo del v0
Un bot Telegram, ospitato su Hetzner, che a comando:
1. Recupera le conversazioni Callbell con attività recente (finestra temporale, default 6h).
2. Le passa a Claude per un triage a tre livelli:
   - DA GESTIRE SUBITO (allarme)
   - IN CORSO (giornale di bordo: rassegna narrativa delle conversazioni presidiate)
   - RUMORE DI FONDO (info generiche, ignorabile)
3. Restituisce su Telegram TRE modalità di output:
   - VOCALE rapido riassuntivo (audio) — SINTETICO: solo urgenti + una frase di panoramica
   - SCHEMA testuale elaborato — giornale di bordo COMPLETO
   - TABELLA operativa (testo formattato) — giornale di bordo COMPLETO
4. Confronta il triage con quello precedente (MEMORIA, vedi sotto) e salva su Supabase.

Il v1 (futuro, fuori scope qui) sarà una PWA mobile-first dedicata.

## Le due profondità di output
Un SOLO triage strutturato alla base, due livelli di resa:
- VOCALE = allarme sintetico (l'utente ascolta in auto/in mezzo ai terrari, non può leggere).
- SCHEMA/TABELLA = giornale di bordo completo (quando ha tempo di guardare lo schermo).
Non tutte le modalità portano la stessa profondità: è una scelta di design, non una mancanza.

## Dettaglio proporzionale alla temperatura
Il triage NON tratta tutte le conversazioni allo stesso modo. Alloca le parole dove serve:
- Routine (orari, info) → mezza riga.
- Conversazione calda (frustrazione, sollecito) o clinicamente delicata → due-tre righe di contesto.
Come un buon caposala che indugia dove c'è polpa e sorvola dove non ce n'è. Questo va istruito
esplicitamente nel prompt: la tendenza naturale del modello è dare a ogni voce lo stesso peso.

## Paletto etico (NON negoziabile)
Il triage descrive lo STATO DELLE CONVERSAZIONI, non giudica l'OPERATO DELLE PERSONE.
- OK: "la sig.ra Rossi aspetta ancora risposta" (stato del cliente).
- NO: "Giulia è in ritardo", "Martina è lenta" (giudizio sull'operato).
Lo strumento dà consapevolezza, non è sorveglianza sul lavoro delle colleghe. Il prompt sta
sempre dal lato dello stato. È una sfumatura che cambia il clima se le colleghe lo percepiscono.

## Memoria tra un triage e l'altro (nel v0, a due livelli)
Ogni run si confronta con lo stato del run precedente. Salto da "fotografia" a "film".
- LIVELLO BASE (affidabile, fa il lavoro pesante): confronto di stato tra run.
  "Questa conversazione era scoperta anche l'ultima volta", "il cliente aspetta da due run",
  "questa è nuova rispetto a stamattina". Robusto, nessuna interpretazione fragile.
- LIVELLO RAFFINATO (prudente, sperimentale nel v0): promesse scadute.
  Dedurre da "le confermo entro due ore" che esiste una scadenza implicita e segnalarla quando
  il tempo è passato. Potente ma a rischio FALSI ALLARMI: va tenuto conservativo (segnala solo
  se la promessa è esplicita E il tempo è chiaramente passato). Meglio prudente che zelante:
  un triage che grida "scaduto!" a sproposito perde la fiducia dell'utente al terzo errore.

## Contesto di dominio
Clinica veterinaria specializzata in animali esotici e aviari. Le urgenze vanno lette con quel
filtro (un coniglio che non mangia da 12h è un'emergenza, non una banalità).
Tre dimensioni di giudizio del triage:
- PRESIDIO: chi sta gestendo (operatore/nota recente = presidiata; ultimo msg cliente da tempo = scoperta)
- URGENZA CLINICA: dedotta dal testo
- TEMPERATURA EMOTIVA: frustrazione/solleciti/reclami del proprietario

I tag e le note delle colleghe sono usati come INDIZIO quando presenti, MAI come unica fonte
(uso irregolare confermato dall'utente).

## Stack
- Backend: Python su Hetzner
- Telegram Bot API (python-telegram-bot)
- Callbell REST API (https://api.callbell.eu/v1) — piano Chat Management Plus richiesto
- Anthropic API per il triage (modello: claude-opus-4-8 mentre si tara il prompt; valutare
  Sonnet a prompt stabile). Una sola chiamata per triage.
- TTS per il vocale: DA DECIDERE — verificare se Leggo AI espone endpoint server-side;
  in v0 ammesso un TTS provvisorio, con Leggo AI agganciato in v1
- Supabase per storico + MEMORIA (vedi dev_notes per lo schema)
- UI Italiano, codice in Inglese

## Portabilità futura (source adapter)
Il triage NON deve sapere che sotto c'è Callbell. Il recupero conversazioni passa per un
"source adapter" che traduce i dati grezzi di qualsiasi fonte in un formato interno neutro
(lista di messaggi con mittente, testo, timestamp, ruolo). Oggi si scrive l'adattatore Callbell;
domani un adattatore WhatsApp-diretto/altro BSP, senza toccare il cervello del triage.
callbell_client è UNA implementazione di un'interfaccia, non il cuore del sistema.

## Vincoli noti (vedi dev_notes.md)
- Chiavi API SOLO server-side, mai esposte
- In v0 NIENTE webhook: solo pull a comando
- Paginazione Callbell e marcatura messaggi in/out da verificare su risposta reale
- Dati clinici + proprietari identificabili: minimizzare, valutare pseudonimizzazione

## Stato attuale
Punto di partenza. Esistono due script standalone come prototipo concettuale
(callbell_export.py e callbell_triage.py) da cui riusare la logica di fetch e il prompt di triage.
