# cronohelper

Paste a Hungarian food-delivery weekly summary, edit the week in a preview,
write it into your Cronometer diary — plus a fixed breakfast on every day.

One container, one page, no external services beyond Cronometer itself.

---

## Setup

```bash
cp .env.example .env      # fill in your Cronometer credentials
docker compose up -d
```

Then open <http://127.0.0.1:8080>.

Paste the weekly summary, check the preview, press **Import**.

### Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `CRONOMETER_USERNAME` | yes | — | Cronometer account email |
| `CRONOMETER_PASSWORD` | yes | — | Cronometer password |
| `TZ` | no | `Europe/Budapest` | Container timezone |
| `CRONOMETER_ACCOUNT_TZ` | no | — | Forces the account timezone used to stamp entries. Only needed if the zone Cronometer reports for your account is wrong |
| `PORT` | no | `8080` | Host port |
| `BIND_ADDR` | no | `127.0.0.1` | Host interface to publish on |
| `DATA_DIR` | no | `/data` | Where the SQLite DB and session token live |
| `LOG_LEVEL` | no | `INFO` | Python log level |

The two credentials are the only secrets. They are read from the environment,
used solely to obtain a session token, and are never written to disk, never
logged, and never sent to the browser. There is a test that asserts this
(`test_no_endpoint_leaks_credentials`).

**This app has no authentication of its own.** It publishes on loopback by
default. Set `BIND_ADDR=0.0.0.0` only on a trusted LAN, and never expose it to
the internet without an authenticating proxy in front.

---

## Running on Unraid

Use `docker-compose.unraid.yml`. It differs from the default compose file in
three ways that all matter on Unraid: it runs as `99:100` (`nobody:users`),
bind-mounts `/mnt/user/appdata`, and publishes on all interfaces.

**The uid is the part that bites.** The image runs as uid 10001. An appdata
directory owned by `nobody:users` is not writable by that uid, so the container
starts, answers `/healthz` happily, and then returns 500 on the first request
that touches SQLite. Running as `99:100` against a correctly-owned directory
fixes it; the app writes nothing outside `/data`, so any uid works.

Copy the project to a share on the Unraid box (`/mnt/user/projects/cronohelper`
or similar — **not** inside the appdata directory), including your `.env`, then
over SSH:

```bash
cd /mnt/user/projects/cronohelper

mkdir -p /mnt/user/appdata/cronohelper
chown -R 99:100 /mnt/user/appdata/cronohelper

docker compose -f docker-compose.unraid.yml up -d --build
```

Reachable at `http://<unraid-ip>:8080`. It appears in the Docker tab like any
other container; `docker compose -f docker-compose.unraid.yml logs -f` for logs.

No registry is needed — the image is built on the Unraid box from the source
you copied. If you would rather build on your workstation, `docker save
cronohelper:latest | ssh root@unraid docker load` also works; drop `--build`
from the compose command afterwards.

The **Compose Manager** plugin from Community Applications gives the same thing
through the GUI if you prefer, pointing it at the same file.

### Portainer

Use `docker-compose.portainer.yml`, **not** the Unraid one.

*Stacks → Add stack → Repository*, compose path `docker-compose.portainer.yml`,
and put the credentials in Portainer's **Environment variables** panel:
`CRONOMETER_USERNAME`, `CRONOMETER_PASSWORD`, `TZ`, `PORT`. Portainer clones
and builds the image itself, so redeploying is a button after a `git push` —
no SSH.

The two compose files differ in exactly one thing that matters here.
`docker-compose.unraid.yml` uses `env_file: .env`, which requires a literal
file next to it; `.env` is gitignored, so a Portainer stack built from the repo
fails before starting anything:

```
failed to resolve services environment: env file /data/compose/2/.env not found
```

Loading variables into Portainer's UI does not fix that — the UI feeds `${VAR}`
substitution, not `env_file`. `docker-compose.portainer.yml` declares the
credentials under `environment:` instead, which is what the UI panel actually
populates. It is also the better arrangement: the password lives in Portainer
rather than in a plaintext file on a share.

Missing variables fail the deploy with a message naming the variable, rather
than starting a container that dies at login.

The `mkdir` and `chown` above still have to be done once by hand — Portainer
does not do it for you, and without it the container answers `/healthz` and
returns 500 on everything else.

### Two cautions

- **Credentials on a share.** `.env` holds your Cronometer password in plain
  text. Make sure the share you copy it to is not exported over SMB/NFS, or
  keep the source on `/mnt/user/appdata` (root-only) and accept the slightly
  odd layout.
- **No authentication.** This publishes on the LAN with nothing in front of it.
  Do not forward the port through your router. If you want it reachable from
  outside, put it behind Tailscale or an authenticating reverse proxy.

### Moving your existing data across

If you have already imported from your workstation, copy the ledger over or the
Unraid instance will happily re-import the same week — the SQLite database is
what makes imports idempotent:

```bash
docker cp cronohelper:/data/cronohelper.sqlite3 ./
scp cronohelper.sqlite3 root@unraid:/mnt/user/appdata/cronohelper/
ssh root@unraid chown 99:100 /mnt/user/appdata/cronohelper/cronohelper.sqlite3
```

Leave `session.json` behind — the new instance will log in once and write its
own.

---

## Running outside Docker

Requires [uv](https://astral.sh/uv); it fetches its own CPython, so no system
Python is needed.

```bash
make dev      # http://127.0.0.1:8080
make test
```

On Windows, where `make` does not exist and the Makefile's recipes are POSIX:

```powershell
.\tasks.ps1 dev
.\tasks.ps1 test
```

---

## Why Python 3.14 and not 3.12

`cronometer-api-mcp` declares `requires-python = ">=3.14"` on every published
version, and it means it: `client.py` uses PEP 758 unparenthesized
`except A, B:` on three lines (137, 392, 426). That is a **`SyntaxError`** on
3.13 and below, not a warning. The package cannot be imported on 3.12 at all.

The alternative was vendoring a patched copy of 1034 lines of upstream code and
re-applying the patch on every upgrade — the opposite of keeping the blast
radius to one file. So the container is `python:3.14-slim` and upstream is used
as published.

---

## How dishes are matched to Cronometer foods

Dishes are **never** matched against Cronometer's public food database. The
names are Hungarian and the macros are known exactly, so each unique dish
becomes a custom food where one serving equals the listed macros.

Resolution runs this ladder, never skipping a step:

1. **Local cache.** SQLite, keyed on the normalized dish name
   (lowercased, whitespace-collapsed, `ED<n>` code stripped, trailing `*`
   stripped, `[ETK] ` prefix stripped, `(YYYY-MM)` version suffix stripped).
   Keyed on the *name*, not the code — the same dish reappears under a
   different code each week. A cache hit costs zero API calls.
2. **An existing food on your account.** A cache miss does not mean the food
   does not exist: you may have made it by hand, or the volume may have been
   wiped. See the spike below for how this works.
3. **Create**, only if 1 and 2 both miss. Everything this app creates is
   prefixed `[ETK] `, so app-created foods are greppable in your food list,
   accidental duplicates are obvious, and there is a clean bulk-delete path.

### Macro conflicts

A resolved food is **never adopted without checking its macros.** If the stored
nutrition differs from the pasted values by more than **2%** on any macro, the
cache is not bound to it, nothing is logged, and the dish is surfaced in the
preview as a conflict with both sets of numbers side by side. You choose:

- **Use existing** — adopt that food and remember it.
- **Create new version** — make a new food named `[ETK] <dish> (2026-09)`.
- **Link** — pin the dish to a specific Cronometer food id.

A wrong adopt here would poison every future week silently, which is the worst
thing this app could do, so it refuses to guess.

### Energy is set explicitly

The delivery site's macros do not reconcile with its own calorie figure —
`19.9×4 + 5.2×4 + 6.8×9 = 161`, but it says 174. Letting Cronometer recompute
energy from macros drifts the daily total by several percent, so the stated
kcal is written explicitly (nutrient id 208).

---

## The food-resolution spike

Whether your own custom foods are retrievable through the mobile API was
undocumented, so it was tested against a real account before any resolution
code was written. **Result: searchable, and identifiable as mine.**

Searching for a hand-made food named `Fitt májgaluskaleves` returned:

```json
{
  "id": 53609159,
  "name": "Fitt májgaluskaleves",
  "source": "Custom",
  "measureId": 175719477,
  "translationId": 60352053,
  "src": 6,
  "measureDisplayName": "1 Serving"
}
```

1. **Does it come back?** Yes.
2. **Is it identifiable as mine?** Yes, twice over. Search results carry
   `source: "Custom"`; the public databases use `CFCD`, `CNF`, `CRDB`,
   `CoFID`, `FDCBranded`, `IFCDB`, `NCCDB`, `NUTTAB`, `USDA`. And `get_food`
   additionally returns `owner`, the numeric user id, which is checked against
   the logged-in user.
3. **Exact or ranked-fuzzy?** **Ranked-fuzzy.** The query also returned two
   `Fitt májgaluska leves` entries (with a space). Resolution therefore
   exact-matches the normalized name and never trusts `results[0]`.

There is **no "list my custom foods" endpoint** anywhere in the client —
search is the only route.

Three other findings the code depends on:

- **Accent-folding the query reduces recall** (3 results → 1). The backend
  handles diacritics fine, so the verbatim query is primary and the folded one
  is only a fallback. Names are compared NFC-normalized but *unfolded* —
  folding both sides risks merging genuinely different dishes.
- **`create_custom_food` always returns `measure_id: None`.** It is hardcoded
  upstream. The created food must be read back with `get_food` to learn its
  real measure id, or nothing can be logged against it.
- Nutrients are stored **per 100 g**; a measure's `value` is its gram weight.

Re-run the spike any time:

```bash
make spike FOOD="Fitt májgaluskaleves"          # or .\tasks.ps1 spike "..."
```

It only reads, and never prints credentials.

---

## Idempotency

Pasting the same email twice writes once.

Every diary entry written is recorded in SQLite (`date`, `diary_group`,
`food_id`, `servings`, `cronometer_entry_id`). Before writing, the app counts
what is already recorded for a `(day, group, food)` triple and writes only the
shortfall; the rest come back as `skipped`.

Counting rather than upserting is deliberate: **two portions of the same soup
on the same day is legitimate**, so a unique key on the triple would silently
drop the second one. Import Friday with one portion and then again with two,
and exactly one new entry is written.

Breakfast is idempotent the same way.

---

## The preview

`/api/parse` is pure — text in, structured preview out, no Cronometer contact,
nothing written. The result is a draft the **browser** owns and mutates.
`/api/import` writes exactly the edited payload it is handed and never
re-parses the original text.

- Every lunch row is **one portion**. A `2 adag` line becomes two independently
  movable rows, because the extra portion is usually eaten on another day.
- Drag a row between day columns, or use its date field (keyboard and phone).
- The two days after the pasted range are offered empty and ready to receive a
  drop — moving a portion onto Saturday is a first-class action.
- Days already imported are visually distinct before you click anything, and
  dropping onto one warns you.
- **Breakfast is on for every day**, toggleable per day, independent of whether
  that day has lunch. A day is skipped only when it has no rows *and* no
  breakfast.
- Parse errors point at the line number and say what was expected. If there is
  even one error, **no** rows are returned — a half-parsed week rendered as a
  preview is indistinguishable from a correct one.

Keyboard: paste parses automatically, <kbd>Ctrl</kbd>/<kbd>⌘</kbd>+<kbd>Enter</kbd> confirms.

---

## HTTP surface

| Endpoint | Writes to Cronometer? | Purpose |
|---|---|---|
| `POST /api/parse` | no | Text in, structured preview out. Pure |
| `POST /api/resolve` | no | Resolve each dish to a food; surfaces conflicts before anything is committed |
| `POST /api/import` | **yes** | Writes the confirmed payload; returns a per-entry result array |
| `POST /api/foods/link` | no | Bind a dish name to a Cronometer food id permanently |
| `GET /api/history` | no | Recent imports and every date already written to |
| `GET /api/config` | no | The fixed breakfast constants |
| `GET /healthz` | no | Container health |

`/api/resolve` is not in the original spec but is what lets a macro conflict
appear in the preview rather than halfway through a write. It reads from
Cronometer and writes nothing to it.

Partial failure is normal: one bad entry never aborts the rest, and every entry
gets its own `created` / `skipped` / `failed` status.

---

## Tests

```bash
make test      # or .\tasks.ps1 test
```

Everything runs against a fake Cronometer client — **never the real API**. The
fake reproduces the real payload shapes captured during the spike, including
`create_custom_food` returning `measure_id: None` and search returning
ranked-fuzzy near-misses.

Covered: the parser on the full sample (multi-comma dish names, the colon
anchor, `2 adag` expanding to two one-portion rows, decimal commas, spaced
thousands separators, NFD input, every failure mode with its line number);
resolution (cache hit, adopt-don't-create, create, macro conflict and both of
its resolutions, public-database and other-owner foods rejected, the fuzzy
near-miss); idempotency (twice writes once, partial re-import writes only the
shortfall); a moved portion landing on its new date and that date getting
breakfast; and that no endpoint leaks credentials.

---

## Recovering when Cronometer changes their API

This talks to a private, undocumented, reverse-engineered API. It *will* break
eventually. It is built so that recovery is contained:

**Everything Cronometer-specific is in `app/cronometer.py`.** Nothing else in
the app imports the upstream client. That file is the only one to fix.

When something breaks:

1. **Read the error.** Upstream failures are translated into specific messages,
   not stack traces — "Cronometer rejected the credentials", "Cronometer is
   rate-limiting this account", "Cronometer's response was not in the expected
   shape — the mobile API has probably changed".

2. **Re-run the spike.** `make spike FOOD="<a food you own>"` dumps the raw
   `find_food` and `get_food` responses. If `source` is no longer `"Custom"`,
   or `owner` is gone, or the nutrient ids moved, you will see it immediately.
   Compare against the payloads recorded above.

3. **Check upstream.** `uv lock --upgrade-package cronometer-api-mcp`. The
   pinned version is `0.2.1`. Read its `client.py` rather than its README —
   the method names differ (`search_food`, not `search_foods`).

4. **Fix `app/cronometer.py`.** The adapter is ~280 lines and the surface it
   exposes to the rest of the app is five methods: `search`, `get_food`,
   `create_food`, `add_entry`, `user_id`. Keep those signatures and nothing
   else needs touching. `tests/fake_cronometer.py` documents the payload
   shapes the adapter expects.

5. **If login starts failing,** do not retry in a loop — Cronometer rate-limits
   login aggressively. The session token is cached in the volume at
   `/data/session.json` and is only refreshed on a 401/403. Delete that file to
   force one clean re-login.

### Data recovery

The volume holds the food-id cache and the written-entry ledger. If you lose it:

- Nothing is duplicated in Cronometer *unless* you re-import the same week —
  the ledger is what makes imports idempotent, so a wiped ledger means a
  re-import writes again.
- The food cache rebuilds itself: every previously created food is still on
  your account with its `[ETK] ` name, so resolution finds and re-adopts it
  rather than creating duplicates. There is a test for exactly this
  (`test_a_created_food_is_adopted_on_the_next_week`).

### Bulk-deleting everything this app created

Every food it made is prefixed `[ETK] `. Search your Cronometer food list for
`[ETK]` to find them all.
