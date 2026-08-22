# runbook.md — deploy e gestione del bot su `vps-agenti`

Come mettere e tenere in piedi il bot Telegram di triage (`msg-triage`) come servizio
systemd sul VPS `vps-agenti`. Il deploy lo esegue Tommaso a mano; questo documento è la
checklist di riferimento.

Il servizio è un **processo sempre attivo in long polling** (nessun webhook, nessuna porta
in ascolto): parla in uscita con Callbell, Anthropic e Telegram, logga su stdout → journald.
La unit di riferimento è versionata in [`deploy/msg-triage.service`](../deploy/msg-triage.service).

## Convenzioni (assunte da questa checklist)
- Utente Linux dedicato **`msgtriage`**, senza sudo. Possiede repo, venv e `.env`.
- Repo clonato in **`/home/msgtriage/msg-triage`** via **deploy key dedicata**.
- Venv nel repo: **`.venv`** (Python 3.12).
- **`.env` nella root del repo**, permessi **0600**, di proprietà di `msgtriage`.
- Le operazioni di sistema (systemctl) le fa l'account di Tommaso con `sudo`; le operazioni
  sui file dell'agente si fanno diventando l'utente: `sudo -u msgtriage -i`.

> **Regola d'oro:** la configurazione viene letta **solo allo startup**. Ogni modifica a
> `.env` (o al codice) richiede `sudo systemctl restart msg-triage` per avere effetto.

---

## A. Provisioning una-tantum

1. **Utente dedicato** (senza sudo), se non esiste già:
   ```
   sudo adduser --disabled-password --gecos "" msgtriage
   ```

2. **Deploy key + clone.** Come `msgtriage`, generare una chiave dedicata, registrarla come
   *deploy key* del repo su GitHub (read-only basta), poi clonare:
   ```
   sudo -u msgtriage -i
   ssh-keygen -t ed25519 -f ~/.ssh/msg-triage-deploy -N ""   # poi incollare la .pub come deploy key
   cat >> ~/.ssh/config <<'EOF'
   Host github-msgtriage
     HostName github.com
     User git
     IdentityFile ~/.ssh/msg-triage-deploy
     IdentitiesOnly yes
   EOF
   git clone git@github-msgtriage:Tomtokio/msg-triage.git ~/msg-triage
   cd ~/msg-triage
   ```

3. **Venv + install editable.** L'install **deve** essere editable: il triage engine legge
   `docs/triage_system_prompt.md` all'import e quel file non è impacchettato nel wheel — un
   install non-editable crasherebbe all'avvio.
   ```
   uv venv --python 3.12
   uv pip install -e .
   ```
   Senza `uv` sulla macchina, equivalente:
   ```
   python3.12 -m venv .venv
   .venv/bin/pip install -e .
   ```

4. **Segreti.** Copiare il template e compilare i **6 valori richiesti** (più `LOG_LEVEL`
   opzionale), poi bloccare i permessi:
   ```
   cp .env.example .env
   nano .env            # compilare i valori
   chmod 600 .env
   ```
   Variabili richieste al boot: `CALLBELL_API_KEY`, `ANTHROPIC_API_KEY`,
   `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_ID` (id numerico Telegram),
   `SUPABASE_URL`, `SUPABASE_KEY`. Le ultime due sono richieste **anche se** Supabase non è
   ancora usato: se mancano, l'app non parte — lasciare `unused` finché non si applica la
   migration (§ E). Opzionali: `LOG_LEVEL` (default `INFO`) e le tre `TELEMETRY_*` (§ F);
   se le `TELEMETRY_*` mancano, la telemetria è semplicemente spenta e il bot parte uguale.
   > `LOG_LEVEL=DEBUG` **non** fa ricomparire le righe `HTTP Request` di `httpx`: quel
   > logger è fisso a WARNING perché l'URL di `getUpdates` contiene il token del bot in
   > chiaro. Non si perde niente di utile — Callbell e Supabase passano da `requests` e
   > non erano comunque visibili a INFO.

5. **Smoke test in foreground** (ancora come `msgtriage`, prima di installare il servizio):
   ```
   .venv/bin/python -m msg_triage
   ```
   Atteso nei log: `VetTriage — secrets loaded: ...` e
   `VetTriage bot avviato (long polling). Comando: /triage [ore].`
   Fermare con `Ctrl-C`, poi `exit` per tornare all'account amministratore.
   > Non lasciare questo processo attivo mentre parte il servizio: Telegram ammette **un solo**
   > consumer di `getUpdates`, due poller in parallelo danno errore 409 (Conflict).

6. **Installare e avviare la unit** (dall'account con sudo):
   ```
   sudo cp /home/msgtriage/msg-triage/deploy/msg-triage.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now msg-triage
   ```

7. **Verifica end-to-end:**
   ```
   sudo systemctl status msg-triage          # deve risultare active (running)
   sudo journalctl -u msg-triage -f          # deve mostrare "bot avviato (long polling)"
   ```
   Poi da Telegram (dall'utente in whitelist) inviare `/triage`: devono arrivare **tre**
   messaggi — schema, tabella, vocale.

---

## B. Update / redeploy (checklist standard)

```
sudo -u msgtriage -i
cd ~/msg-triage && git pull
# se sono cambiate le dipendenze (pyproject.toml):
uv pip install -e .            # oppure: .venv/bin/pip install -e .
# se serve, aggiornare .env
exit

sudo systemctl restart msg-triage           # OBBLIGATORIO dopo modifiche a .env o codice
sudo systemctl status msg-triage
sudo journalctl -u msg-triage -n 50
```

> `vet_agents_telemetry` **non** è in `pyproject.toml` (è pinnata a un tag, § F): quindi
> `uv pip install -e .` non la aggiorna. Per cambiarne versione serve il comando esplicito.

---

## C. Rollback

```
sudo -u msgtriage -i
cd ~/msg-triage
git log --oneline -n 10
git checkout <sha-precedente>
uv pip install -e .            # solo se le dipendenze erano cambiate
exit
sudo systemctl restart msg-triage
sudo systemctl status msg-triage
```

---

## D. Troubleshooting

- **Il servizio non parte.** `sudo journalctl -u msg-triage -n 50`. Se è un problema di
  configurazione, il log riporta `Configuration error: Missing required environment
  variables: ...` con i **nomi** delle variabili mancanti (mai i valori). Controllare
  contenuto e permessi di `.env`.

- **Stato `failed` con "start-limit-hit".** Una config errata ha esaurito i 5 tentativi in
  300s. Correggere `.env`, poi:
  ```
  sudo systemctl reset-failed msg-triage
  sudo systemctl start msg-triage
  ```

- **Il servizio non parte, `ExecStart` sospetto.** Verificare che il flag sia `-m` (trattino
  ASCII) e **non** `—` (em-dash): un copia-incolla può averlo trasformato. In alternativa
  usare la forma senza `-m`: `ExecStart=/home/msgtriage/msg-triage/.venv/bin/msg-triage`.

- **Crash all'import** con errore sul prompt `docs/triage_system_prompt.md`. L'install non è
  editable o manca `docs/` nel checkout. Reinstallare con `uv pip install -e .` da un repo
  completo (mai un wheel nudo).

- **Modifiche a `.env` senza effetto.** Manca il restart: `sudo systemctl restart msg-triage`.

- **Da Telegram non risponde niente.** Il bot risponde **solo** all'id in
  `TELEGRAM_ALLOWED_USER_ID`; ogni altro utente non riceve nulla per progetto. Verificare che
  l'id in `.env` sia quello giusto. Controllare anche che non ci sia un secondo poller attivo
  (es. uno smoke test dimenticato in foreground → 409 Conflict nei log).

---

## E. Supabase — applicare la migration e accendere la persistenza

Una tantum. Finché non si fanno questi passi il bot funziona identico a prima: non salva
niente e non se ne lamenta (un `logger.debug`, non un errore).

Progetto: **`agents-telemetry`** (ref `hmbyxyyckvfbbfcjyhad`, eu-west-1).
Schema: **`msg_triage`**. File: [`migrations/0001_msg_triage_schema.sql`](../migrations/0001_msg_triage_schema.sql).

> **L'ordine conta:** prima l'SQL, poi gli Exposed schemas, poi la chiave. Saltando il
> secondo passo PostgREST risponde `PGRST106` qualunque cosa faccia il codice.

1. **SQL.** Dashboard Supabase → progetto `agents-telemetry` → SQL Editor → incollare tutto
   `migrations/0001_msg_triage_schema.sql` → Run. Gira come ruolo `postgres`. Crea schema,
   quattro tabelle, indici, RLS attiva senza policy e i grant per `service_role`.

   Poi, **nello stesso modo e nell'ordine**,
   [`migrations/0002_proposals_lifecycle.sql`](../migrations/0002_proposals_lifecycle.sql)
   (T10/PR2): aggiunge `proposals.decided_at`, il vincolo di unicità
   `(contact_id, tag)` su `system_tags` e l'indice del ripescaggio. Le due tabelle di T10
   sono vuote finché non si accende `ENABLE_PROPOSALS`, quindi si applica senza rischio —
   ma va applicata **prima** che il codice giri, o il primo `/triage` a flag acceso
   fallisce su `decided_at` (`PGRST204`). Vedi § G.

2. **Exposed schemas.** Settings → API → Data API → **Exposed schemas**: aggiungere
   `msg_triage` a fianco di quelli già presenti (non sostituirli), Save. Senza questo passo
   lo schema custom non esiste per PostgREST.

3. **La chiave: service_role in formato JWT LEGACY (`eyJh…`), non `sb_secret_…`.**
   Settings → API Keys → sezione delle chiavi legacy/JWT (su alcuni progetti va prima
   riabilitata). Il formato nuovo non porta il claim di ruolo che PostgREST usa quando cambia
   profilo su uno schema custom: il sintomo è un errore di permessi che *sembra* un grant
   mancante, e si perde un pomeriggio a cercarlo nella migration.

4. **`.env` sul VPS.** Come `msgtriage` (`sudo -u msgtriage -i`, file `~/msg-triage/.env`,
   permessi 0600):
   ```
   SUPABASE_URL=https://hmbyxyyckvfbbfcjyhad.supabase.co
   SUPABASE_KEY=eyJh…          # service_role, formato legacy
   ```
   poi, dall'account con sudo: `sudo systemctl restart msg-triage` — la config si legge
   **solo** allo startup.

5. **Verifica.** Da Telegram: `/triage`. Nei log deve comparire
   `Saved triage run <uuid> (N conversation states)`:
   ```
   sudo journalctl -u msg-triage -n 50 | grep -Ei 'supabase|saved triage run'
   ```
   Poi nella SQL Editor:
   ```sql
   select id, created_at, window_hours, n_conversations
   from msg_triage.triage_runs order by created_at desc limit 1;

   select contact_id, nome, gruppo, urgenza, presidio, specie, last_message_at
   from msg_triage.conversation_states
   where run_id = (select id from msg_triage.triage_runs order by created_at desc limit 1);
   ```

6. **Copertura del campo `specie`** (da rilanciare ogni tanto, non solo al primo giro):
   ```sql
   select count(*) filter (where specie is not null) as con_specie,
          count(*)                                   as totale,
          round(100.0 * count(*) filter (where specie is not null) / nullif(count(*), 0)) as pct
   from msg_triage.conversation_states
   where created_at > now() - interval '7 days';
   ```
   **Una buona percentuale di `NULL` è attesa e non è un bug dell'estrazione.** Il modello
   marca la specie quando la nomina («la **tartaruga** Bianca») e non la nomina sempre («la
   dimissione del coniglio»). Il codice cerca il marcatore in `motivo`, poi in
   `stato_sintetico`, poi in `azione_suggerita`: quella catena di ripiego è già la
   mitigazione. **Se la percentuale è bassa, la cura è rinforzare la regola in
   `docs/triage_system_prompt.md`, non toccare l'estrazione** — allentare il parsing
   produrrebbe specie sbagliate, che sono peggio di nessuna specie.

### Se qualcosa non va

Il triage arriva **comunque** su Telegram: la persistenza è best-effort e un fallimento è un
WARNING nel journal, mai un errore per l'utente. Il messaggio dice quale dei tre passi manca:

| Nel log | Cosa manca |
|---|---|
| `PGRST106` (schema non exposed) | passo 2 |
| `42501` / permission denied | passo 3 (chiave nel formato sbagliato) |
| `PGRST205` (tabella non trovata) | passo 1, oppure PostgREST non ha ricaricato: rilanciare `notify pgrst, 'reload schema';` |
| `ConnectionError` / `Timeout` | rete del VPS verso Supabase |

I log riportano status e `code`/`message` di PostgREST, **mai** `details`/`hint`: quelli
riecheggerebbero la riga rifiutata, cioè nomi di clienti, dentro il journal.

### Retention

Non attiva: lo snippet è in fondo al file della migration, commentato. Consigliata —
90 giorni per `triage_runs` (`conversation_states` cade a cascata), 12 mesi per le tabelle
di T10. Da lanciare a mano quando serve.

---

## F. Telemetria — installare la libreria e accendere il monitoraggio

Una tantum. Serve a far vedere l'agente sulla dashboard degli agenti: che il processo è
vivo (heartbeat ogni 5 minuti), com'è andato ogni run di triage e quanto è costata la
chiamata a Claude. Finché non si fanno questi passi il bot funziona **identico**: la
libreria è fail-silent per contratto, e il codice la importa in modo protetto.

Libreria: **`vet_agents_telemetry` v0.1.1**. Contratto:
[`docs/telemetry_api_contract.md`](telemetry_api_contract.md).
Progetto Supabase: lo stesso di § E (`agents-telemetry`), schema **`telemetry`** — diverso
da `msg_triage`, tabelle diverse, chiave letta da variabili sue. La libreria **non** riusa
`SUPABASE_URL`/`SUPABASE_KEY`.

1. **Installazione, pinnata al tag.** Come `msgtriage`, dentro il venv del repo:
   ```
   sudo -u msgtriage -i
   cd ~/msg-triage
   uv pip install git+ssh://git@github.com/Tomtokio/vet-agents-telemetry.git@v0.1.1
   ```
   Non va in `pyproject.toml` apposta: la versione la si sceglie a mano, un tag alla volta.

   > **Accesso SSH.** `msgtriage` ha una deploy key dedicata anche su
   > `vet-agents-telemetry`: quella di `msg-triage` è scoped su quel repo e non basta.
   > Se in `~/.ssh/config` la seconda key sta dietro un alias, l'URL dell'install deve
   > usare l'alias al posto di `github.com`. Sintomo se qualcosa non torna:
   > `Permission denied (publickey)` durante l'install.

   **Controllare cosa è stato davvero installato** — un tag può puntare al commit
   sbagliato, e il sintomo (costi tutti a zero) si vede solo settimane dopo:
   ```
   .venv/bin/python -c "import vet_agents_telemetry as t; from vet_agents_telemetry \
   import pricing; print(t.__version__, pricing.compute_cost('anthropic', \
   'claude-opus-4-8', input_tokens=1000000, output_tokens=1000000))"
   ```
   Atteso: `0.1.1 30.0`. Se stampa `0.1.0` o `None`, il modello non è nella pricing
   table: la telemetria funziona lo stesso, i token sono giusti, ma ogni riga di
   `agent_usage` avrà `cost_usd = 0` e comparirà un evento `pricing_missing`.

2. **`.env` sul VPS.** Come `msgtriage` (`~/msg-triage/.env`, permessi 0600):
   ```
   TELEMETRY_SUPABASE_URL=https://hmbyxyyckvfbbfcjyhad.supabase.co
   TELEMETRY_SUPABASE_KEY=eyJh…      # service_role dedicata agli agenti
   TELEMETRY_ENABLED=true            # opzionale: è già il default
   ```
   Poi, dall'account con sudo: `sudo systemctl restart msg-triage`.

3. **Verifica.** All'avvio, senza fare niente su Telegram:
   ```sql
   select type, severity, metadata, created_at from telemetry.agent_events
   where agent_id = 'msg-triage' order by created_at desc limit 5;

   select status, updated_at from telemetry.agent_heartbeats where agent_id = 'msg-triage';
   ```
   Attesi un `agent_started` e un heartbeat `ok`. Poi `/triage` da Telegram: quattro eventi
   con lo **stesso `job_id`** (`processing_started` → `conversations_fetched` →
   `triage_judged` → `processing_completed`) e una riga di costo:
   ```sql
   select model, operation, input_tokens, output_tokens, cost_usd
   from telemetry.agent_usage where agent_id = 'msg-triage'
   order by created_at desc limit 1;
   ```

4. **Spegnerla.** `TELEMETRY_ENABLED=false` in `.env` + restart: no-op completo, nessun
   log, nessuna rete. Non serve disinstallare niente.

### Se qualcosa non va

Il triage arriva **comunque** su Telegram, sempre: nessuna chiamata di telemetria può far
fallire o rallentare un run. I problemi si vedono solo nel journal, sotto il logger
`vet_agents_telemetry`:

```
sudo journalctl -u msg-triage -n 100 | grep -i telemetry
```

| Nel log | Cosa succede |
|---|---|
| `vet_agents_telemetry non installata: telemetria non attiva` | passo 1 non fatto (o venv sbagliato) |
| `Telemetry disabled: TELEMETRY_SUPABASE_URL / …_KEY not set` | passo 2 non fatto |
| `auth failure (status 401/403)` | chiave sbagliata o revocata |
| `timed out (>3.0s)` / `Supabase unreachable` | rete del VPS; l'evento è perso, nient'altro |
| nessuna riga | tutto a posto, oppure `TELEMETRY_ENABLED=false` |

**Cosa NON finisce nella telemetria:** nomi di clienti, numeri di telefono, testo dei
messaggi, testo del digest, prompt e risposte del modello. Solo id di riferimento,
conteggi, durate e codici di errore — vedi `CLAUDE.md § Telemetria`.

---

## G. T10 — proposte organizzative (`ENABLE_PROPOSALS`)

**Stato a PR3: le proposte arrivano su Telegram e il tap ✅ scrive su Callbell.** Il flag
accende tutta la catena: il blocco *fatti di stato* nella singola chiamata LLM (PR1), le
*regole deterministiche* che ne ricavano le proposte (PR2), la consegna coi bottoni e
l'esecuzione (PR3). Serve **anche** Supabase vero: senza database non c'è idempotenza — un
rifiuto verrebbe dimenticato e la stessa proposta tornerebbe a ogni run — quindi il codice
preferisce non proporre niente e lo dice una volta nel log.

Il path di lettura resta incapace di scrivere: `build_adapter()` e `build_read_client()` non
concedono `allow_writes`. L'unica porta è `build_write_client()`, e la apre solo
`msg_triage/proposal_executor.py` dopo un tap confermato su una proposta che esiste sul DB.

> ⚠️ **Regola aperta: nessun ✅ su una proposta `Ricoverato`, per ora.** Il fatto `ricovero`
> non è mai stato esercitato sul dato reale (l'A/B del 06/08 non lo copriva, e da quando il
> flag è acceso non ne è ancora nata una), e `Ricoverato` è l'unico tag che non si toglie mai
> a tempo: messo per sbaglio resta lì. Tutto il resto si può tappare — ❌ non scrive niente,
> una rinomina si disfa rinominando indietro, `Da gestire subito` e `Dimissione oggi` hanno
> la loro rimozione programmata. Quando la prima proposta `Ricoverato` arriva, si legge senza
> toccare i bottoni: è quello l'A/B. Contesto in `docs/triage_system_prompt.md`, ultime "Note
> per lo sviluppatore".

### Come si comporta, in pratica

- Dopo i tre messaggi del triage arriva **una proposta per messaggio**, con ✅ Applica /
  ❌ Ignora. Arrivano **tutte quelle pending e mature**, non solo quelle nate adesso: la coda
  parte dalle più vecchie, e vale anche quando la finestra è vuota.
- **Ogni proposta si consegna una volta sola.** Anche se resta lì senza risposta per giorni,
  non torna: i bottoni del messaggio funzionano finché non li tocchi.
- **✅** → si rilegge il contatto su Callbell, si scrive, si verifica l'eco, e il messaggio
  diventa «✅ …». **❌** → «❌ Ignorata», e Callbell non viene toccato affatto.
- **Un secondo tap sullo stesso bottone** risponde «Proposta già gestita» e non riscrive
  niente: la difesa sta sul database, non in memoria.
- **A flag spento** un tap risponde solo «Proposte disattivate» e lascia il messaggio com'è:
  riaccendi il flag e lo stesso bottone torna a funzionare.
- Se una proposta non si può eseguire, il messaggio lo dice per esteso. In particolare
  «*… stato non registrato in system_tags — verificare a mano*» significa che **su Callbell
  la scrittura c'è**: manca solo la riga sul nostro database.

### I tre tag gestiti

`Ricoverato` · `Dimissione oggi` · `Da gestire subito` — scritti così, con le maiuscole e
gli spazi che vedi, perché li leggono le colleghe nella UI di Callbell.

> **Un tag è "nostro" solo se esiste la sua riga in `msg_triage.system_tags`, mai per
> nome.** `Ricoverato` è byte-identico a quello che le colleghe usano a mano: il nome non
> distingue niente. Conseguenza accettata: le loro istanze la regola semantica non le
> tocca mai; ci arriverà solo la rete dei 14 giorni (PR4).

La verifica è ricontrollata **al momento del tap**, non solo al momento della proposta: se
la riga in `system_tags` è sparita nel frattempo, la rimozione viene rifiutata senza toccare
Callbell.

### Accendere

1. La migration `0002` applicata (§ E, passo 1). Senza, il primo run fallisce. **PR3 non
   ha aggiunto migration**: `telegram_message_id`, `decided_at` ed `executed_at` erano già
   in `0001`/`0002`, in attesa di chi le scrivesse.
2. Nel `.env` sul VPS (come `msgtriage`, file `~/msg-triage/.env`, permessi 0600):
   `ENABLE_PROPOSALS=true`, con `SUPABASE_URL`/`SUPABASE_KEY` veri.
3. `sudo systemctl restart msg-triage` — la config si legge solo allo startup.

### Prima di accendere: il giro a secco delle regole

`ricovero` e `dimissione` non sono mai stati esercitati sul dato reale. Prima di lasciare
le regole vicino al database, si guardano girare sui fatti veri — **senza scrivere niente,
da nessuna parte**:

```
.venv/bin/python scripts/smoke_triage.py --real 6 --facts --proposals
```

Stampa le proposte che nascerebbero, con payload e motivo. Cosa cercare: nessun nome
inventato, nessuna data uscita dal nulla, `Ricoverato` solo dove qualcuno ha scritto
davvero di una degenza. Le rimozioni di `Ricoverato` **non possono comparire qui** per
costruzione (richiedono una riga in `system_tags`, che il dry-run non legge).

### Verifica dopo il primo `/triage` a flag acceso

Nei log deve comparire la riga coi soli conteggi (mai nomi, mai `contact_id`):

```
sudo journalctl -u msg-triage -n 80 | grep -i 'proposte t10'
```

Poi nella SQL Editor:

```sql
select created_at, contact_id, tipo, payload, motivo, stato, matures_at
from msg_triage.proposals
where stato = 'pending'
order by created_at desc;
```

> **Il test di idempotenza va fatto a pochi minuti di distanza, non a ore.** Un secondo
> `/triage` sulla **stessa finestra** non deve creare nuove righe: è la prova che la
> proposta già `pending` blocca il duplicato. Ma se nel frattempo la finestra si è spostata
> e pesca conversazioni diverse, righe nuove sono l'esito **corretto** e il test non
> dimostra niente. Due giri ravvicinati, stessa `/triage N`, e si confronta il conteggio:
> ```sql
> select count(*) from msg_triage.proposals where created_at > now() - interval '30 minutes';
> ```

Su Callbell, finché non tocchi un bottone: **nessun tag cambiato, nessun nome cambiato.**
Le proposte nascono e si consegnano senza scrivere niente; la scrittura è il tap.

### Verifica del ciclo completo, dopo il primo tap

```sql
select tipo, stato, created_at, telegram_message_id, decided_at, executed_at
from msg_triage.proposals order by created_at desc limit 20;

select * from msg_triage.system_tags;
```

Cosa deve tornare:

| Cosa hai fatto | Cosa dice il DB | Cosa dice Callbell |
|---|---|---|
| niente (solo consegna) | `pending`, `telegram_message_id` valorizzato | invariato |
| ❌ | `rifiutata` + `decided_at` | invariato |
| ✅ su un `tag_add` | `eseguita` + `executed_at`, riga in `system_tags` col `proposta_id` | tag aggiunto, **gli altri tag intatti** |
| ✅ su una `rename` | `eseguita` + `executed_at` | nome nuovo, byte per byte |
| ✅ su `Dimissione oggi` / `Da gestire subito` | in più una riga `tag_remove` `pending` con `matures_at` alle 07:00 di Roma | — |
| ritap sullo stesso bottone | niente cambia | niente cambia |

Il controllo che conta di più è la terza riga della colonna di destra: **gli altri tag del
contatto devono essere ancora tutti lì**, con la stessa grafia (l'ordine può cambiare, il
contenuto no). È il motivo per cui prima di ogni PATCH si rilegge il contatto e dopo si
confronta quello che Callbell ha salvato con quello che gli è stato mandato.

Le rimozioni programmate restano `pending` finché non maturano; poi le consegna il primo
`/triage` successivo (il *job* che le consegna da solo è PR4).

Una riga da tenere d'occhio nei log, perché risponde a una domanda ancora aperta — se
Callbell riordini la lista dei tag quando la riscrive:

```
sudo journalctl -u msg-triage | grep -i 'riordinato i tag'
```

Non è un guasto (il contenuto è verificato tag per tag, l'ordine non lo legge nessuno), ma
se compare vuol dire che l'ordine non si conserva, ed è un fatto da annotare in
`dev_notes.md` come gli altri.

### Il censimento dei `Ricoverato` che restano fuori

Da fare una volta, col flag acceso e qualche run alle spalle: quanti `Ricoverato` esistenti
sono delle colleghe e quindi la regola semantica non li toglierà mai. È il numero che serve
per decidere se valga la pena "adottarli" in `system_tags` con una migrazione una tantum,
invece di lasciarli alla rete dei 14 giorni.

```sql
select count(*) from msg_triage.system_tags where tag = 'Ricoverato';
```

confrontato col totale dei contatti che portano il tag su Callbell (il dry-run di
`scripts/cleanup_stale_tags.py` lo stampa: `=== TAG 'Ricoverato' — N contatti dal filtro ===`).
Se la differenza è grossa, se ne riparla; finché non c'è il numero, la scelta è un'opinione.

> ⚠️ **`scripts/cleanup_stale_tags.py` in modalità `--esegui-davvero` non va più lanciato
> con T10 vivo**: strapperebbe tag di sistema scavalcando le proposte, e lascerebbe le
> righe di `system_tags` a dire il falso. Il dry-run resta utile come censimento.

### Spegnere

`ENABLE_PROPOSALS=false` + restart. Il triage torna identico a prima, byte per byte: niente
proposte nuove, niente consegna, e un tap su un bottone già in chat risponde solo «Proposte
disattivate» senza scrivere niente. Le righe `pending` restano dove sono, coi loro bottoni
ancora validi: riaccendendo il flag riprendono a funzionare, senza mettere le mani sul DB.

Quello che è già stato scritto su Callbell **non torna indietro da sé**: lo spegnimento
ferma le azioni future, non annulla quelle passate. Per disfare un tag messo per sbaglio si
toglie a mano dalla UI di Callbell e si cancella la riga corrispondente in
`msg_triage.system_tags` (altrimenti quella riga continua a dire che il tag è nostro).

---

## Comandi rapidi

| Azione | Comando |
|---|---|
| Stato | `sudo systemctl status msg-triage` |
| Log in coda | `sudo journalctl -u msg-triage -f` |
| Ultimi 50 log | `sudo journalctl -u msg-triage -n 50` |
| Restart (dopo `.env`/deploy) | `sudo systemctl restart msg-triage` |
| Stop / start | `sudo systemctl stop|start msg-triage` |
| Diventare l'agente | `sudo -u msgtriage -i` |
