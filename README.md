<h1 align="center">Unstuck</h1>

<p align="center">
  <strong>Ask several AI models the same question at once, then make them argue about it.</strong>
</p>

<p align="center">
  Runs entirely on free API tiers &middot; no payment card &middot; no paid model &middot; no GPU
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="python">
  <img src="https://img.shields.io/badge/licence-MIT-green" alt="licence">
  <img src="https://img.shields.io/badge/cost-%240-brightgreen" alt="cost">
  <img src="https://img.shields.io/badge/models-6%20families-orange" alt="models">
</p>

---

Most AI tools give you an answer. The hard part of planning anything is usually
not the answer -- it is not knowing which questions to ask.

Unstuck runs five models with **conflicting instructions** against the same
prompt, merges what survives, then puts the result through a Drafter / Critic /
Judge debate. Every mode ends with a section naming the decisions you still have
to make.

![architecture](docs/architecture.svg)

## See it work

**[→ Read a real run](examples/sample-run.md)** -- unedited, exported from the
app's own database.

The interesting part is not that the answers are good. It is that they
*disagree*. On "should I build a habit tracker", one model opened with *"ship a
single-page app with localStorage today"* while another opened with *"don't
build a tracker, build a retention engine -- 99% of habit apps fail on churn"*.
Four models given the same prompt would have agreed with each other and the
merge would have been mush.

## Where they disagreed

The most valuable thing a run produces is not the answer -- it is the place the
models reached *opposite* conclusions, because that is where the easy consensus
answer would have been wrong. That used to be buried in prose inside the merged
markdown, so the interface hid the product's best output.

The synthesis now emits those points as structured data alongside its prose,
and the UI shows them above the answer: each contested point, the opposing
positions side by side, and how it was resolved.

It costs no extra API call -- the synthesis already knows what it resolved, and
a second extraction call would spend a request on a tier that allows 8000
tokens a minute. A malformed block costs the panel and never the answer:
`engine/disagreement.py` forgives trailing commas, missing labels, prose inside
the fence and outright garbage, degrading to "no panel, full answer intact".

## The three modes

| Mode | What runs | Time |
|---|---|---|
| **Quick** | 5 models in parallel, then one merges them | ~40s |
| **Debate** | Drafter writes, Critic attacks, Drafter revises &times;3, Judge rules | ~4min |
| **Full** | Quick, then the debate runs on the synthesised plan | ~7min |

## Design decisions worth defending

**No agent framework.** The fan-out is `asyncio.gather`; the debate is a `for`
loop over a transcript. LangGraph or AutoGen would add an abstraction to learn
and a layer to debug through, and buy nothing at this size.

**No two decision seats may run the same model.** A model attacking or judging
its own output shares the blind spots that produced the flaw. Seats also spread
across *providers*, because two models from one vendor fail together when that
vendor has an outage.

**Framings are not tied to a vendor.** A seat whose provider has no key keeps
its angle of attack and moves to any reachable model, so the tool degrades in
quality rather than going dark.

**Your phone can drop off Wi-Fi mid-run.** Every event is persisted with a
sequence number; a reconnecting client asks for `?after=<seq>` and replays only
what it missed.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
```

Unstuck runs on **free API tiers**. None of these need a payment card, and you
do not need all of them -- Unstuck uses whatever keys it finds and skips the
rest. One key works; three or four is much better, because the entire value of
the synthesis comes from the answers being genuinely different.

| Provider | Key | Sign up |
|---|---|---|
| **Groq** (start here) | `GROQ_API_KEY` | <https://console.groq.com/keys> |
| **Google AI Studio** | `GEMINI_API_KEY` | <https://aistudio.google.com/apikey> |
| **Cerebras** | `CEREBRAS_API_KEY` | <https://cloud.cerebras.ai> |
| **GitHub Models** | `GITHUB_TOKEN` | <https://github.com/settings/tokens> - fine-grained, **must grant the `Models` permission** |
| **Mistral** | `MISTRAL_API_KEY` | <https://console.mistral.ai> (phone verification) |

**Verified live 2026-09-27**, because model names go stale fast:

- **Groq**: `openai/gpt-oss-120b`, `openai/gpt-oss-20b`, `qwen/qwen3.8-27b`. The
  rest of its catalogue is speech and safety classifiers, not chat models.
- **Gemini**: the 2.x line is retired for new accounts. Working: `gemini-3.8-flash`,
  `gemini-3.5-flash`, `gemini-flash-latest`. The `pro` models 429 immediately on
  the free quota. Google lists models as `models/x` but accepts bare `x`.
- **Cerebras**: only `gpt-oss-120b` and `qwen-3.8-27b` -- the *same families*
  Groq offers. It buys a separate rate-limit bucket, not a different voice.
- **GitHub Models**: a token without the `Models` permission returns a
  plain-text `200 OK` instead of a completion, which looks like success.

Put the keys in `.env`, then check everything before starting:

```bash
python check_key.py
```

That prints which providers are configured and validates every model slug
against its provider's live catalogue -- without ever printing a key. Fix
anything it flags, then run:

```bash
python -m uvicorn server:app --host 0.0.0.0 --port 8000
```

Open <http://localhost:8000>.

## Signing up

An account needs a working email address. The flow is three steps -- address,
code, then name and password -- in that order, because asking for a password
before the code means throwing it away when the code fails, and a form that
loses what you typed is a form people abandon.

```
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=you@example.com
SMTP_PASSWORD=an-app-password
```

Without these, codes are printed to the server console instead of sent. That is
not a stub: it is how the flow is developed without a mail account, and how a
self-hoster runs a private instance for one person. A Gmail App Password works
and needs no domain. It also grants full SMTP access to that mailbox, so it
belongs in `.env` and a dedicated address is the safer choice.

A six-digit code is one in a million, which sounds safe and is not -- unthrottled,
a script tries every one of them in minutes. So the code is only as good as the
limits around it, and all of them are enforced server-side:

| | |
|---|---|
| Codes expire | 15 minutes |
| Wrong guesses | 5, then the code dies |
| Resend | once a minute, 5 an hour per address |
| Storage | SHA-256, salted with the address it was sent to |
| Comparison | constant time |

Binding the hash to the address matters: without it a code issued for one
address verifies another. The final step re-checks the code, or it would be an
unauthenticated "create an account for any address you like" endpoint.

`ALLOW_LEGACY_SIGNUP=true` reopens the old username-and-password route, which
otherwise returns 410. Leave it off on anything public -- if it is reachable,
verification is optional, and optional verification is decoration.

## Letting other people use your instance

Unstuck can run on each user's own API keys instead of yours. Sign in, open
**API keys** in the sidebar, and paste a key per provider.

```
BYOK_ONLY=true
```

With that set, runs use only the signed-in user's keys and never fall back to
the host's. Set it on any instance other people can reach: one free tier allows
roughly 80 full runs a *day* across everyone, and reselling free-tier capacity
is how vendor accounts get closed.

Keys are encrypted with Fernet under a key derived per user via HKDF, so one
user's ciphertext cannot be read with another's derived key and cannot be moved
between accounts. They are never returned by the API in full -- only a masked
fragment. `COUNCIL_SECRET` is persisted rather than regenerated at boot,
because a master key that only lives in memory forces every user to retype
four API keys after every restart. Back it up; losing it makes stored keys
unrecoverable.

A key is **verified with a real API call before it is stored**. A credential
that authenticates but lacks permission is the worst kind of failure -- a
GitHub token missing the `Models` scope returns a plain-text `200 OK` that
parses as an empty answer, which looks exactly like success.

Two settings are not optional once strangers can reach it.

```
REQUIRE_ACCOUNTS=true        # or the app is open until the first signup
USER_SPEND_CAP_CENTS=100     # lifetime paid spend per account, in cents
```

`REQUIRE_ACCOUNTS` closes a fallback that is right for a laptop and wrong for
the internet: with no accounts and no shared password the app serves everyone,
so a freshly deployed instance is unauthenticated until somebody signs up.
Signup and login stay open, or the first account could never be created.

`USER_SPEND_CAP_CENTS` is a lifetime cap, not a daily one, because a daily cap
is a rate rather than a limit -- the same person can spend it again every
morning. It bounds paid spend only; with `PREMIUM_SEATS` off every run uses
free providers and the cap never fires.

## Optionally paying for the two seats that need it

Everything runs free by default. If you want better output, the cheapest place
to spend money is also the most valuable: **Synthesis and the Judge**. Those two
read everything and have to resolve contradictions, which is what free models do
worst. The five proposers only need to produce a distinctive opinion, which they
already do well, so paying for them buys the least.

Measured over 18 real runs, those two seats see about 6,000 tokens in and 3,500
out per Full run:

| Model | Per Full run | Runs per $5 |
|---|---|---|
| `claude-sonnet-5` | $0.047 | ~106 |
| `claude-haiku-4.5` | $0.024 | ~212 |
| `gpt-5-mini` | $0.0085 | ~588 |

```
PREMIUM_SEATS=true
PREMIUM_MODEL=anthropic/claude-sonnet-5
```

**Top up before switching it on.** With a zero balance every Synthesis fails
with a 402, which is worse than the free model it replaced -- so the default is
off and a test asserts it stays off.

## Decision memos

Every finished run exports as a markdown memo: the question, what is still
undecided, where the models disagreed and how it resolved, the full answer, and
an honest record of which seats answered, which failed and which notes were
recalled.

A chat log is not a record. Three weeks after deciding something you want to
know what you decided, what the alternatives were and what you knew at the
time, and scrolling a transcript gives you none of that quickly. A memo is a
file: diffable, greppable, readable without running anything.

**Save to vault** writes it into your notes, which closes the loop -- a memo is
indexed like any other note, so a decision made in March is retrieved
automatically when you ask something related in June.

No model is called to build a memo. It is assembled from what the run already
produced, so it costs nothing and cannot fail on a rate limit.

## Long-term memory (optional)

Point Unstuck at a folder of markdown notes and it searches them before every
run, so you stop pasting the same project context into every prompt. An
Obsidian vault is exactly such a folder -- no plugin, no API, no sync service.

```
VAULT_PATH=C:/Users/You/Documents/My Vault
```

Notes are split at markdown headings, because a section is a unit of meaning:
retrieving `## Constraints` whole is useful, while 500 characters straddling
two topics is noise. Search uses SQLite's built-in FTS5 rather than embeddings
-- no embedding API, no vector store, no re-index pipeline, and a keyword match
is debuggable in a way a cosine score is not.

**Privacy.** Retrieved text is sent to the model providers. So the vault is
opt-in, `.private`, `.secret`, `.obsidian`, `.trash`, `.git`, `node_modules`
and `templates` are never indexed (add more with `VAULT_EXCLUDE`), and every
run displays exactly which notes it used. Nothing leaves quietly.

Retrieval is capped at ~3000 characters. That budget competes with the answer
for the same 8000 tokens/minute window, so unbounded recall would turn every
run into a `413`.

## Using it from your phone

`--host 0.0.0.0` is what makes this possible — without it the server only
accepts connections from the machine it runs on.

**Same Wi-Fi (easiest).** Find this machine's LAN address:

```bash
ipconfig
```

Look for `IPv4 Address` (something like `192.168.1.x`), then open
`http://192.168.1.x:8000` on your phone. Both devices must be on the same
network. Windows Firewall will probably prompt the first time — allow it for
private networks.

**Create an account first.** Open the app and use *Create account*; the first
account adopts any history that existed before accounts did. Anyone on the same
network can otherwise open the URL and spend your API quota.

Passwords are hashed with `hashlib.scrypt` (standard library, memory-hard, no
compiled dependency). Sessions live in SQLite, not in memory, so restarting the
server does not sign you out. Login is throttled to 8 failed attempts per
address per 5 minutes -- an unthrottled form defeats any password a human will
actually type.

Conversations are scoped to their owner in SQL rather than filtered afterwards,
because a filter forgotten at one call site leaks someone else's history while a
missing `WHERE` clause simply returns nothing.

`COUNCIL_PASSWORD` still works as a single shared gate, but only while no
account exists -- so self-hosters are not locked out by upgrading.

**From anywhere.** Use a tunnel:

```bash
cloudflared tunnel --url http://localhost:8000
```

That prints a public HTTPS URL. **Set `COUNCIL_PASSWORD` in `.env` before you do
this** — otherwise anyone who finds the URL is spending your OpenRouter credit.

Either way the PC has to stay awake and running the server.

**With the PC off.** There is a `Dockerfile` and a `fly.toml` in the repo:

```bash
fly auth login
python fly_deploy.py
```

That creates the app, provisions a volume, pushes the secrets from `.env` over
stdin and deploys. Two settings matter on anything the internet can reach.
`REQUIRE_ACCOUNTS=true` closes the open-access fallback -- a fresh instance has
no accounts, so without it the app is unauthenticated until the first person
signs up. `COUNCIL_DB` must point at the mounted volume, because the app
directory is part of the image and is replaced on every deploy.

The conversation lives on the server, so phone and desktop see the same history.
Pick up on your phone exactly where you left off on the desktop.

---

## Why a slow seat no longer holds up the run

Measured across stored runs rather than guessed. Four proposers would finish in
33 seconds and the fifth would sit silent for 153, holding the whole run open;
synthesis emitted reasoning at 24 seconds and its first answer token at 107,
costing 243 seconds of a 564-second run on its own.

Those are two different problems and one threshold cannot tell them apart, so
there are two:

```
HEDGE_AFTER=18           # no output at all: stuck behind a rate limit
HEDGE_CONTENT_AFTER=55   # reasoning, but still no answer: alive, just slow
```

Past either, a second provider is started for the same seat and the two race.
The first usable answer wins and the loser is cancelled. **Nothing is dropped** --
both attempts are producing the same seat's answer, so the panel still fills;
it just fills from whichever vendor was awake.

A seat that is already streaming is never hedged. Spending a second provider's
quota to overtake a model that is delivering is pure waste, and silence is the
signal that the retry loop has it. When a hedge does start, the primary is
muted: at that moment it has produced no answer by definition, so the panel is
clean and whichever side wins fills it in one piece rather than interleaving
two different answers into one box.

Honest limit: this caps the tail. Whether it lowers the *average* run needs
more than one measurement per configuration, and run-to-run variance here is
large -- full runs have ranged 250 to 646 seconds on identical code.

## Light and dark

Both, with a switch in the header, remembered per browser and shared between
the landing page and the app. It follows the system setting until you choose,
and an explicit choice wins from then on.

Every colour is a token; dark lives in `:root` and light overrides the same
names under `[data-theme="light"]`, so no rule is written twice. A script in
`<head>` applies the stored choice before first paint, because reading it
afterwards shows one frame of the wrong theme on every load.

The interface is otherwise monochrome. Saturated colour means one thing -- a
model's stance, agree, partly or dissent -- and both palettes are checked
against WCAG AA, which is why the light stance colours are darker rather than
the same hexes on a pale background.

## What the chat will not tell you

Vendor and model names are stripped from every event on the way out of the
server. The chat shows the seat -- Pragmatist, Skeptic, Judge -- because that
is the part that carries meaning; which vendor happened to answer is an
implementation detail, and naming it invites the reader to grade an answer by
its badge instead of its content.

The redaction happens as an event leaves the server, not in the browser: a
front-end fix leaves the names sitting in the network tab. The database keeps
the full record, so an operator can still tell which model wrote what.

```
SHOW_MODEL_NAMES=true    # put them back, for debugging
```


---

## Changing the models

Two files. `engine/providers.py` lists the providers (base URL + which env var
holds the key). `engine/config.py` assigns each seat a provider and a model:

```python
Agent(key="pragmatist", label="Pragmatist",
      provider="groq", model="openai/gpt-oss-120b", ...)
```

Every provider speaks the OpenAI-compatible protocol, so adding a new one is a
few lines in `providers.py` and nothing else.

**Model slugs go stale.** Providers rename and retire models constantly. If a
run fails with "model slug probably wrong", run `python check_key.py` -- it
names the bad seat. To see what a provider currently offers:

```bash
curl "http://localhost:8000/api/models?provider=groq"
```

The **framings** matter more than the model choice. Four models given an
identical prompt return four similar answers, and merging those produces mush.
Each proposer is pushed toward a different angle on purpose, and the four sit on
four *different vendors* for the same reason. If you change the roster, keep
them in conflict.

Other tunables: `DEBATE_ROUNDS` (3 -- past 4 the Drafter starts agreeing with
everything), token ceilings, `MEMORY_TURNS`, and `MAX_PARALLEL_PER_PROVIDER`.

## Cost and limits

Money: none. Every provider in the default roster has a free tier.

What you pay instead is **rate limits**. A Quick run is 5 calls, Debate is 8,
Full is 12 -- and free tiers cap requests per minute and per day. Spreading the
four proposers across four different providers is what makes the parallel
fan-out work at all; four seats on one provider would trip its per-minute limit
immediately.

Groq's free tier is **8,000 tokens per minute** and 1,000 requests per day. The
per-minute token cap is the binding constraint, not the request count -- which
is why seats sharing a provider run one at a time by default, and why the token
budgets in `config.py` are sized the way they are. A Quick run takes about 20
seconds under that limit.

A 429 is retried automatically (up to 3 times, honouring `Retry-After`) as long
as the seat has not already started streaming. If it still fails, that seat
reports the error and the others carry on.

**Reasoning models need headroom.** `openai/gpt-oss-*` stream their chain of
thought before the answer, and those tokens count against `max_tokens`. Too
small a budget produces an empty response with no error at all -- so the client
detects that case and raises a message telling you to raise the budget. The
reasoning itself is streamed to the UI and shown dimmed while a seat thinks.

## Architecture

```
static/          the UI: two HTML pages, no build step, no framework
  theme.js       light/dark, shared by both pages
server.py        FastAPI: conversations, turns, SSE streaming, auth
db.py            SQLite: conversations, turns, and an append-only event log
check_key.py     preflight: which providers work, which slugs are stale
Dockerfile       }
fly.toml         }  deployment: volume-backed SQLite, scale to zero
fly_deploy.py    }  app, volume, secrets and deploy in one command
engine/
  config.py      the roster and tunables   <- start here
  prompts.py     the personas              <- and here
  providers.py   free providers and their base URLs
  orchestrator.py  runs the seats, and hedges the ones that stall
  verification.py  signup codes: expiry, attempt caps, throttling
  mailer.py        sends them, or prints them when SMTP is unset
  disagreement.py  lifts the structured verdict out of the prose
  orchestrator.py  fan-out/fan-in, and the debate loop
  llm.py         provider-agnostic streaming client
```

**No agent framework.** Feature 1 is `asyncio.gather` over N calls; feature 2 is
a `for` loop over a growing transcript. LangGraph or AutoGen would add an
abstraction to learn and a layer to debug through, and buy nothing at this size.

**One client, many providers.** Everything speaks OpenAI-compatible chat
completions, so `engine/llm.py` handles all of them and only the base URL and
auth header change. Seats whose provider has no key are skipped, not failed.

**Every event is persisted with a sequence number.** The UI streams live over
SSE but the database is the record. A client that drops off — phone locking,
Wi-Fi to cellular, tab backgrounded — reconnects with `?after=<last seq>` and
replays only what it missed. Nothing is lost and nothing arrives twice.

**Free tiers fail constantly, so failure handling is most of the work.**
429 (rate limited), 5xx (provider overloaded -- Gemini does this often),
timeouts and DNS blips are all retried up to 4 times with backoff, honouring
`Retry-After`. A retry only happens before the seat has streamed anything,
since the stream cannot be rewound. 413 is NOT retried: a request too large
never becomes small by waiting.

**Partial failures degrade, they don't cascade.** One dead proposer leaves the
other three. A failed Aggregator returns the raw proposer answers rather than
discarding work you already paid for. A failed Judge returns the last revision
of the plan. Only losing *every* proposer, or the initial draft, fails a run.

---

## What building this actually taught me

Most of the code is failure handling, because free tiers fail constantly:

- **Reasoning models return empty responses when starved of budget.** They
  spend tokens thinking before writing a word, and the share varies by prompt,
  not just by model. Four different families blew four different ceilings, each
  time returning HTTP 200 with no content and no error. The client now detects
  this and retries with a larger budget instead of guessing per model.
- **Thinking arrives two ways** -- as separate `reasoning` deltas, or inline in
  the content wrapped in `<thought>` tags. The inline kind leaks raw thinking
  into the answer and into the next debate round unless stripped.
- **A provider's model listing can include models it will not serve.**
- **A credential can authenticate and still be wrong.** A GitHub token missing
  the `Models` permission returns a plain-text `200 OK` that parses as an empty
  answer -- success-shaped failure, the worst kind.
- **One vendor's outage takes out every seat on it at once**, so decision seats
  are spread across providers, not just across models.

## Tests

Eleven suites, **256 checks**, all offline against fake models. They cost
nothing and need no API key:

```bash
python smoke_test.py         # the engine: all modes, every partial-failure path
python test_server.py        # HTTP, SSE, persistence, reconnect replay
python test_auth.py          # the shared-password gate
python test_accounts.py      # sessions, hashing, per-user isolation
python test_byok.py          # per-user keys, encryption, masking
python test_vault.py         # markdown search
python test_disagreement.py  # parsing the structured verdict
python test_memo.py          # decision memo export
python test_quota.py         # free runs, credit, the spend cap
python test_verification.py  # signup codes and every refusal around them
python test_hedge.py         # racing a stalled seat, and never racing a live one
```

Run them after touching the orchestrator or the event bus. Three bugs they have
caught, none of which were visible without looking:

- concurrent event emits overtaking each other and being silently discarded by
  the subscriber's dedupe, so eight events vanished per run
- `asyncio.wait` on a set containing an already-finished task returns instantly
  every time, which turns a wait into a hot loop -- twice, in two different
  places, in the hedging code
- the decisions panel firing **zero** times in twenty runs, because models split
  their output across two fenced blocks and the parser read only the first
