# CLAUDE.md — msg-triage

## Cos'è questo progetto
Triage intelligente delle conversazioni WhatsApp della clinica (via Callbell).
Legge i messaggi recenti, li giudica, produce un digest a tre livelli.
**Legge soltanto: non risponde mai ai clienti.**

## Documentazione — LEGGILA PRIMA DI QUALSIASI COSA
- `docs/project_state.md` — cos'è, obiettivi, decisioni prese
- `docs/tasks.md` — task T1–T9, ordine e dipendenze
- `docs/dev_notes.md` — convenzioni, vincoli, anti-pattern
- `docs/triage_system_prompt.md` — il system prompt del triage (testo operativo)

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

## Da verificare sul dato reale (non assumere)
1. Schema paginazione Callbell (ipotizzato `data["pagination"]["nextPage"]`) — confermare con curl.
2. Marcatura messaggio in entrata vs uscita (confronto `from` col telefono del contatto).
Vedi `docs/dev_notes.md` per il dettaglio.
