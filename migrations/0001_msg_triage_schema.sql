-- 0001 — msg-triage (T7): storico, memoria tra run, ciclo di vita proposte.
-- Progetto: agents-telemetry (ref hmbyxyyckvfbbfcjyhad).  Schema: msg_triage.
--
-- Applicare A MANO dalla SQL Editor di Supabase (gira come ruolo postgres).
-- Istruzioni complete, nell'ordine giusto: docs/runbook.md § E.
--
-- Una sola migration per tre padroni: lo storico dei run, la memoria tra un run e
-- l'altro (T4, interroga conversation_states per contact_id) e il ciclo di vita di
-- tag e proposte (T10, tabelle che restano vuote finché T10 non arriva).

begin;

create schema if not exists msg_triage;

-- Solo service_role entra nello schema: queste tabelle contengono nomi reali di
-- clienti e informazioni cliniche. anon e authenticated non ricevono nulla.
grant usage on schema msg_triage to service_role;
revoke all on schema msg_triage from anon, authenticated;


-- ===========================================================================
-- triage_runs — un record per esecuzione di /triage: storico + i tre testi resi
-- ===========================================================================
create table msg_triage.triage_runs (
    id              uuid        primary key default gen_random_uuid(),
    created_at      timestamptz not null default now(),
    window_hours    numeric     not null,
    n_conversations integer     not null,
    schema_text     text        not null,
    table_text      text        not null,
    vocal_text      text        not null
);

create index triage_runs_created_at_idx on msg_triage.triage_runs (created_at desc);

comment on column msg_triage.triage_runs.n_conversations is
    'Numero di righe figlie in conversation_states, non di conversazioni recuperate: il triage può ometterne qualcuna.';


-- ===========================================================================
-- conversation_states — una riga per conversazione per run. È QUESTA la tabella
-- che abilita la memoria (T4): si interroga per contact_id, ultimo run per primo.
-- ===========================================================================
create table msg_triage.conversation_states (
    id                        uuid        primary key default gen_random_uuid(),
    run_id                    uuid        not null
                                          references msg_triage.triage_runs (id) on delete cascade,
    created_at                timestamptz not null default now(),
    contact_id                text        not null,
    nome                      text        not null default '',
    gruppo                    text        not null check (gruppo in ('subito', 'in_corso', 'rumore')),
    motivo                    text        not null default '',
    urgenza                   text        not null check (urgenza in ('emergenza', 'alta', 'media', 'bassa')),
    presidio                  text        not null check (presidio in ('presidiata', 'scoperta')),
    temperatura               text        not null check (temperatura in ('alta', 'media', 'bassa')),
    stato_sintetico           text        not null default '',
    azione_suggerita          text        not null default '',
    specie                    text,
    last_message_at           timestamptz,
    promessa_testo            text,
    promessa_scadenza_stimata text
);

-- La query della memoria: "l'ultimo stato salvato per questo contatto".
create index conversation_states_contact_idx
    on msg_triage.conversation_states (contact_id, created_at desc);
create index conversation_states_run_idx
    on msg_triage.conversation_states (run_id);

comment on column msg_triage.conversation_states.created_at is
    'Denormalizzato dal run: rende la query della memoria un solo scan di indice, senza join.';
comment on column msg_triage.conversation_states.specie is
    'Estratta dal marcatore **specie** nel testo del triage. NULL ATTESO in buona parte delle righe: il modello marca la specie quando la nomina ("la **tartaruga** Bianca") e non la nomina sempre ("la dimissione del coniglio"). Tasso basso => si rinforza la regola nel prompt, non l''estrazione.';
comment on column msg_triage.conversation_states.promessa_scadenza_stimata is
    'TESTO, non timestamp: il modello può restituire "entro sera" oltre a una data.';


-- ===========================================================================
-- proposals (T10) — resta VUOTA finché T10 non arriva.
-- ===========================================================================
create table msg_triage.proposals (
    id                  uuid        primary key default gen_random_uuid(),
    created_at          timestamptz not null default now(),
    contact_id          text        not null,
    tipo                text        not null check (tipo in ('tag_add', 'tag_remove', 'rename')),
    payload             jsonb       not null default '{}'::jsonb,
    motivo              text        not null default '',
    stato               text        not null default 'pending'
                                    check (stato in ('pending', 'approvata', 'rifiutata',
                                                     'eseguita', 'fallita')),
    matures_at          timestamptz,
    executed_at         timestamptz,
    telegram_message_id bigint
);

create index proposals_contact_idx on msg_triage.proposals (contact_id);
create index proposals_stato_idx   on msg_triage.proposals (stato);


-- ===========================================================================
-- system_tags (T10) — resta VUOTA. Nessun vincolo di unicità: sarà T10 a decidere
-- se è un registro storico o lo stato corrente dei tag.
-- ===========================================================================
create table msg_triage.system_tags (
    id          uuid        primary key default gen_random_uuid(),
    contact_id  text        not null,
    tag         text        not null,
    applied_at  timestamptz not null default now(),
    proposta_id uuid references msg_triage.proposals (id) on delete set null
);

create index system_tags_contact_idx on msg_triage.system_tags (contact_id);


-- ===========================================================================
-- RLS e permessi. RLS attiva ovunque e NESSUNA policy: anon e authenticated non
-- leggono né scrivono niente. service_role scavalca RLS per progetto — è il ruolo
-- con cui scrive il bot, e la sua chiave vale per tutto agents-telemetry.
-- ===========================================================================
alter table msg_triage.triage_runs         enable row level security;
alter table msg_triage.conversation_states enable row level security;
alter table msg_triage.proposals           enable row level security;
alter table msg_triage.system_tags         enable row level security;

grant select, insert, update, delete on all tables in schema msg_triage to service_role;
revoke all on all tables in schema msg_triage from anon, authenticated;

alter default privileges in schema msg_triage
    grant select, insert, update, delete on tables to service_role;

commit;

-- PostgREST deve rileggere lo schema dopo il commit.
notify pgrst, 'reload schema';


-- ---------------------------------------------------------------------------
-- Retention (NON attiva: da lanciare a mano quando serve, o via pg_cron).
-- 90 giorni per lo storico: la memoria guarda indietro un run solo, e la prosa
-- clinica non ha motivo di restare per anni. 12 mesi per le tabelle di T10, che
-- sono la traccia di cosa è stato scritto su Callbell e non contengono prosa.
--
--   delete from msg_triage.triage_runs  where created_at < now() - interval '90 days';
--   -- conversation_states cade da sé: on delete cascade
--   delete from msg_triage.proposals    where created_at < now() - interval '12 months';
--   delete from msg_triage.system_tags  where applied_at < now() - interval '12 months';
-- ---------------------------------------------------------------------------
