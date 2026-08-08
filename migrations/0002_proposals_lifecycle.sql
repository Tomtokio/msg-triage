-- 0002 — msg-triage (T10/PR2): ciclo di vita delle proposte.
-- Progetto: agents-telemetry (ref hmbyxyyckvfbbfcjyhad).  Schema: msg_triage.
--
-- Applicare A MANO dalla SQL Editor di Supabase (gira come ruolo postgres), PRIMA che
-- il codice di PR2 giri. Istruzioni nell'ordine giusto: docs/runbook.md § E.
--
-- 0001 aveva creato proposals e system_tags vuote, lasciando aperte due scelte che
-- toccava a T10 chiudere. Qui si chiudono. Le tabelle sono ancora vuote: nessun dato
-- da migrare, nessun rischio.

begin;

-- ===========================================================================
-- proposals.decided_at — QUANDO L'UTENTE HA DECISO, che non è quando abbiamo
-- proposto. La riproponibilità a 30 giorni di un tag rifiutato si misura da qui:
-- con created_at si conterebbe il tempo passato dalla PROPOSTA, e una proposta
-- restata in coda una settimana tornerebbe una settimana troppo presto.
-- ===========================================================================
alter table msg_triage.proposals add column decided_at timestamptz;

comment on column msg_triage.proposals.decided_at is
    'Quando l''utente ha deciso (✅/❌); NULL finché è pending. created_at è quando abbiamo proposto: sono due tempi diversi e la riproponibilità a 30 giorni si misura da questo.';


-- ===========================================================================
-- system_tags — 0001 diceva "nessun vincolo di unicità: sarà T10 a decidere se è
-- un registro storico o lo stato corrente". T10 decide: è LO STATO CORRENTE dei
-- tag che abbiamo messo NOI. La storia sta già in proposals, riga per riga, con
-- il suo motivo e il suo esito; duplicarla qui darebbe due fonti che divergono.
--
-- Il vincolo porta anche l'invariante n.1 di T10: un tag è "nostro" solo se esiste
-- la riga qui, MAI per nome. `Ricoverato` è byte-identico al tag che le colleghe
-- usano a mano, quindi il nome non distingue niente.
-- ===========================================================================
alter table msg_triage.system_tags
    add constraint system_tags_contact_tag_key unique (contact_id, tag);

comment on constraint system_tags_contact_tag_key on msg_triage.system_tags is
    'system_tags è lo stato corrente, non uno storico: una riga per coppia (contatto, tag). Rimuovere il tag significa cancellare la riga.';


-- ===========================================================================
-- L'indice del ripescaggio delle proposte programmate (PR4): il job cerca
-- "stato = pending and matures_at <= now()", che senza questo indice è un seq scan
-- ogni trenta minuti.
-- ===========================================================================
create index proposals_stato_matures_idx on msg_triage.proposals (stato, matures_at);

commit;

-- PostgREST deve rileggere lo schema dopo il commit, o la colonna nuova non esiste
-- per l'API (sintomo: PGRST204 su decided_at).
notify pgrst, 'reload schema';
