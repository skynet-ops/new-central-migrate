# New Central Migration Script

`new_central_migrate.py` copies HPE Aruba Networking New Central **Library Profiles**
(roles, policies, WLAN SSIDs, authentication servers, network groups/services,
NTP, RF, AP/switch/gateway system profiles, captive portal, AAA profiles,
aliases, time ranges, and more) from one New Central tenant to another.

It is a single, dependency-free Python 3 script (standard library only — no
`pip install` needed) that talks directly to the New Central REST API using a
bearer token for each tenant. It does not touch, store, or transmit your
tenant data anywhere except between the two tenants you point it at.

This script only copies tenant-wide **Library** objects. It does not assign
those profiles to any site, device group, or physical AP/gateway/switch —
that assignment step still has to be done by hand in the destination
tenant's UI (or scripted separately) after this runs.

## Requirements

- Python 3 (no extra packages)
- An API token for the SOURCE tenant (read access is enough)
- An API token for the DESTINATION tenant (needs write access)
- Each tenant's API base URL (e.g. `https://us2.api.central.arubanetworks.com`)

Tokens are tenant credentials — treat them like passwords. Don't paste them
directly into chat tools, commit them to a repo, or share them over
unencrypted channels.

### Windows

Both scripts are plain standard-library Python with nothing OS-specific in
them, so they run on Windows the same as macOS/Linux. Two differences worth
knowing about, both of which are also good reasons to prefer the GUI
(Option A below) on Windows specifically:

- The command is usually `python` rather than `python3` on Windows (or use
  the `py` launcher: `py migration_gui.py`).
- Setting environment variables for the CLI path (Option B) uses different
  syntax than the `export VAR="value"` shown below — PowerShell:
  `$env:SRC_BASE_URL="..."`, Command Prompt: `set SRC_BASE_URL=...`. The GUI
  avoids this entirely since credentials go into a web form instead.

## Option A: Browser-based GUI (recommended for most people)

`migration_gui.py` is a small local web server (still standard-library-only,
no installs) that wraps the same script behind a form in your browser instead
of environment variables and command-line flags. Both files must be in the
same folder.

```bash
python3 migration_gui.py
```

This starts a server on `http://127.0.0.1:8765` and opens it in your default
browser automatically (if that fails, just open the URL yourself). From
there:

1. Set the export folder (or leave the default).
2. The "Object types covered by this migration" table lists every type this
   tool knows how to migrate — roles, policies, WLANs, NTP, and the rest —
   pulled straight from the script itself, so it can't drift out of sync.
   Its "Items found" column starts out blank ("not exported yet") and fills
   in with real counts as soon as Step 1 finishes, so you can see at a
   glance what actually came from the source tenant (e.g. "Policies (ACLs):
   69 items") before deciding to apply anything.
3. Under "Step 1", enter your source tenant's base URL and token, optionally
   click "Test connection" to confirm they work, then click "Export & dry
   run". Output streams live into the log box at the bottom — read it before
   moving on, same as the CLI's dry-run output.
4. Under "Step 2", enter your destination tenant's base URL and token, then
   click "Apply to destination". You'll get a confirmation prompt first
   since this step writes real changes.

The GUI is a thin wrapper — it calls the exact same `main()` function the
command line does, so everything in this README about phases, `SKIP`/`ERR`
meanings, and known limitations applies identically either way. It only
listens on your own machine (`127.0.0.1`), never on your network, and it
never writes the tokens you type to disk — they're held in memory only for
the duration of one run.

## Option B: Command line

Set tokens as environment variables in your own terminal session rather than
typing them inline in a command you might copy/paste somewhere.

### Step 1 — Export from your SOURCE tenant

This step only reads from the source tenant. It does not need destination
credentials at all, and it makes no changes anywhere.

```bash
export SRC_BASE_URL="https://<your-source-tenant-api-host>"
export SRC_TOKEN="<your-source-tenant-bearer-token>"

python3 new_central_migrate.py
```

Run with no other flags, this:

- Reads every supported object type from the source tenant
- Saves each type to its own JSON file under `./export/` (override the
  location with `--export-dir <path>`)
- Prints a dry-run report: what will migrate, what's being skipped as a
  protected system default, and a full reference-check list of every
  named object one item points at (so you can sanity-check dependencies
  before writing anything)
- Writes nothing to any destination — this is read-only against source

Read that dry-run output before moving on. In particular check the
"WHAT WON'T COME ACROSS" section printed near the end and the reference
check list — some things (secrets, a few object types with no working API
path) need manual attention in the destination regardless of what this
script does.

The `./export/` folder now holds a complete, self-contained snapshot of
your source tenant's config. **Treat these JSON files as sensitive** — they
contain your real WLAN names, RADIUS/DNS server addresses, network
definitions, and similar internal details. Don't share this folder outside
your own migration process.

### Step 2 — Apply to your DESTINATION tenant

Once you've reviewed Step 1's output, apply it. This step only needs
destination credentials — it reads from the export files saved in Step 1,
not from the source tenant again:

```bash
export DST_BASE_URL="https://<your-destination-tenant-api-host>"
export DST_TOKEN="<your-destination-tenant-bearer-token>"

python3 new_central_migrate.py --from-export --export-dir ./export --apply
```

This writes each object to the destination tenant's Library, in an order
that respects dependencies between object types (e.g. roles are created
before the policies that reference them, network groups/services before
the ACLs that reference them, and so on).

Every line of output is one of:

- `OK` — created successfully
- `SKIP (already a protected library default in DEST)` — a built-in Aruba
  default that already exists in every tenant; not a real problem
- `SKIP (already exists in DEST, likely from a prior run)` — you're
  re-running after an earlier partial run; this object is already there.
  By default this script also tries to refresh its content (a follow-up
  PUT with the current source data) in case it was created before some
  dependency it refers to existed yet, so a plain create-or-skip would
  otherwise never fix it — you'll see this reported as
  `OK (refreshed existing object's content in DEST)` when that succeeds.
  This never touches a genuine protected library default (see the next
  line) — only objects this script itself created. Pass
  `--no-refresh-existing` to turn this off and go back to a plain skip.
- `SKIP (secret encrypted with SOURCE tenant's vault key...)` — this
  object's value (usually an Alias holding a RADIUS shared secret) was
  encrypted with the source tenant's own key and can't be decrypted in the
  destination. Create that exact object by hand in the destination with
  its real value, then re-run `--apply` — anything that depended on it
  (e.g. auth servers, and WLANs that depend on those auth servers) will
  then succeed automatically on the same re-run. See "Known limitations"
  below.
- `ERR` — a genuine failure; the exact message tells you what's wrong

If you see `ERR` lines, re-run is safe: this script does not delete or
duplicate-check-bypass anything, so fixing one issue and running the exact
same `--apply` command again will only touch what's still missing. This
includes a `Network error calling ...` `ERR` line (a plain connection
timeout/drop talking to your destination tenant) — this script retries a
network-level failure automatically a couple of times, and if it still
fails, reports it as an `ERR` for that one object and moves on to the
rest instead of stopping the whole run. Just re-run `--apply` afterward to
pick up whatever hit that.

## Object names with spaces (or other unusual characters) no longer crash the run

Confirmed live 2026-09-13: an object whose name contains a space (a
perfectly normal, legitimate name — e.g. an Alias called `ashburn cppm
guest appliance`) used to crash the entire run with
`http.client.InvalidURL: URL can't contain control characters`. This
happened specifically on the follow-up PUT used by the "refresh existing
object" feature and by phase F's role-policy reattach, both of which build
a URL by appending the object's raw name after its collection path
(`.../aliases/ashburn cppm guest appliance`) — an unescaped space in a URL
path is invalid and the underlying `http.client` library raises instead of
just sending a bad request.

Fixed by percent-encoding every object name used this way before it goes
into a URL, so `ashburn cppm guest appliance` now correctly becomes
`ashburn%20cppm%20guest%20appliance` in the actual HTTP request line. As a
second line of defense, `api_call()` also now catches any exception type at
all (not just network timeouts) and turns it into a normal `ERR` for that
one object instead of ever letting the whole run die again — so even an
exception type nobody has seen yet can't repeat this failure mode. If you
were hit by this crash before, just re-run `--apply` again: it's safe, and
this time it will get past the object that stopped it and keep going
through the rest of the run, including retrying anything (like a policy
refresh) that came after the point where it previously died.

## "System Default GW Policy" is now recognized as a protected default up front

Confirmed live 2026-09-13: a Policy named exactly `System Default GW Policy`
has an ordinary-looking name/description, so it wasn't caught by the
protected-default heuristics ahead of time. It sailed through to a create
attempt (`400 ... already exists in Library`, the same message this script
already knows means "an earlier run created this" — see the "already
exists" bullet above), which triggered the refresh-via-PUT logic, which
THEN revealed its true nature: `400 Validation failure: The policy name
System Default GW Policy is restricted.` Nothing was ever at risk — both
the create and the PUT were rejected by the destination itself before
anything could be overwritten — but this wasted two API calls and printed
a confusing double-failure message instead of a clean skip. This exact
name is now listed in `SYSTEM_OBJECT_EXACT_NAMES`, so it's recognized and
skipped up front as `SKIP (system-generated)`, same as `sys_allow_all` /
`sys_deny_all`. If you hit an object of some OTHER type with a similarly
"ordinary-looking name that turns out to be restricted", the fix is the
same: add its exact name to `SYSTEM_OBJECT_EXACT_NAMES` near the top of
`new_central_migrate.py`.

## L2 VLANs: `GET .../l2-vlan` returns 400 on some tenants

Confirmed live 2026-09-13: on at least one source tenant, the L2 VLAN read
itself fails before this script ever gets to classify or migrate anything:
`400 {'errorCode': 'HPE_GL_ERROR_BAD_REQUEST', 'message': 'Invalid module
name/configuration root element in the URL'}`. This is different from
"0 found" (a valid empty list, like `mesh`/`alg` on this same tenant) — it's
the endpoint itself being rejected, so no `l2-vlan.json` export file gets
written at all, and `--apply` (or `--from-export --apply`) then reports
`[FAIL] --from-export: no saved file for L2 VLANs`. Not yet root-caused: it
could mean this particular tenant/API version doesn't expose L2 VLANs at
`/network-config/v1alpha1/l2-vlan` (a different slug may be needed), or
that the token's scope doesn't cover this module. If you actually have L2
VLANs to migrate, this is worth chasing down the same way the Network
Groups/Network Services "wrong slug" cases were solved earlier (see the
next section) — try a plain `GET` against a couple of nearby slug
candidates (`l2-vlans`, `vlans`, `l2vlans`) directly against SOURCE and see
which one, if any, returns 200. If you have no L2 VLANs configured in the
source tenant, this can be ignored — nothing else in the run depends on it.

## If you hit a new "Cannot find in library ... referred in ..." error

This means some object in your tenant references another named library
object that this script doesn't yet migrate — a type it hasn't seen before.
This has happened twice already during this script's development (Network
Groups, then Network Services), so it's a known, expected possibility on a
different tenant's data, not a sign anything is broken.

To fix it:

1. Read the exact error: `Cannot find in library '<name>' of type
   '<TYPE>' referred in '<object>' of type '<other-type>'`. The `<TYPE>`
   names the missing object type.
2. Find its REST endpoint. New Central's paths generally follow
   `/network-config/v1alpha1/<plural-of-type-name>` — e.g. `aruba-net-group`
   lives at `/net-groups`. Try the plural of the type name and close
   variants against your source tenant with a simple GET request until one
   returns HTTP 200.
3. Once you have the working path and its response's envelope key (the
   JSON key holding the array of objects), add a new entry to the
   `OBJECT_TYPES` list near the top of `new_central_migrate.py`, in an
   early phase (`"A"` is usually right, unless the new type itself
   references roles or other objects, in which case pick a later phase —
   see the phase table in the script's own docstring for the dependency
   order).
4. Re-run Step 1 (export) so the new type gets pulled, then Step 2 (apply)
   again.

## Automating secrets instead of retyping them by hand

By default, anything vault-encrypted in the source (auth server shared
secrets, WPA personal-security passphrases, and secret-holding Aliases
like a RADIUS key) is stripped before writing, and the output tells you to
retype it by hand in the destination UI. If you'd rather have this tool
write the real value in directly — no manual UI step at all — supply it
up front:

**GUI:** paste JSON into the "Secret values (optional)" box under Step 2,
e.g.:
```json
{"alias:NAM_RADIUS_KEY": "the real shared secret",
 "auth-server:US_RADIUS_CHI_VIP1": "the real shared secret"}
```
It's held in memory and a private temp file for the length of that one
run only, then deleted immediately after — never written to the export
folder or anywhere permanent.

**CLI:** save the same JSON to a file and pass `--secrets-file`:
```bash
python3 new_central_migrate.py --from-export --export-dir ./export --apply \
    --secrets-file ./secrets.json
```

Keys are `"<list_key>:<name>"` — the dry-run output and the reference-check
report both print the exact names to use (e.g. `NAM_RADIUS_KEY` under
`[Phase A] Aliases:`, `US_RADIUS_CHI_VIP1` under `Authentication servers`).
If an object has more than one secret field, use a nested object instead
of a plain string: `{"alias:X": {"default-value.some.path": "real value"}}`
— the dry-run output shows you the exact dotted path.

This file/box holds real secrets — treat it like a password file. Don't
commit it to version control, and delete the CLI version once you're done
with it (the GUI's temp copy deletes itself automatically after each run).

Every field this covers is detected two ways: the auth-server/WLAN fields
this script already knows about by name, AND, generically, any string
value anywhere in an object that matches Aruba's own vault-encrypted
format (`vault:v<N>:...`) — this is what catches a secret-holding object
this script has no prior knowledge of, like an Alias, without needing a
code change first.

## If Phase F says every role was rejected as a "protected library default"

If the apply output ends with a `*** WARNING: ALL N role(s) above were
rejected...` line, this destination tenant's API is refusing to modify
(create OR update) built-in-named roles at all — not just to create them.
Confirmed live: this affects the PUT used to reattach custom policies
(e.g. `logon` → `vpnlogon`/`logon-policy`), not just the earlier POST-based
create attempt. When this happens, this script cannot get those policy
attachments onto those specific roles via the API on this tenant — it has
to be done by hand in the destination tenant's New Central UI: open each
role the warning lists, and attach the policies the "Reference check"
section of the dry-run output shows for it (e.g. `Roles 'logon' -> policies
= 'vpnlogon' (Policy (ACL))`). The same restriction likely also applies to
the content of any individual Policy object that shares a name with an
Aruba default (e.g. if you customized `logon-policy`'s rules beyond
Aruba's stock version, that customization can't be pushed here either,
for the same reason) — check those in the destination UI too.

To tell a genuine platform restriction apart from a token/permission-scope
issue with just your API credentials, try hand-editing one of the affected
roles in the destination tenant's UI yourself. If the UI lets you do it,
the block is specific to what your `DST_TOKEN` is allowed to do — worth
asking your GreenLake admin whether a broader-scoped token is available.
If the UI also refuses to let you edit it, this is a hard, byproduct-of-
this-tenant limitation with no API workaround.

## Known limitations (documented in the script's own docstring too)

- **Bandwidth Contracts, 802.1X auth profiles, MAC auth profiles, AP
  Certificate Usage**: no working API endpoint was ever found for these
  four types. They're stripped from what they're attached to and flagged
  in the reference-check report so you can recreate them by hand.
- **Central NAC / "cloud-auth" WLANs**: WLANs using New Central's Central
  NAC (RADIUS-as-a-Service) integration reference auto-provisioned objects
  that only exist once Central NAC has been onboarded in that specific
  tenant via its own UI flow. These WLANs likely need that onboarding done
  in the destination first, or a manual recreate.
- **Secrets**: shared secrets and WPA personal passphrases are
  vault-encrypted with the source tenant's own key and are meaningless in
  the destination. By default these are stripped and printed as a
  checklist to retype by hand in the destination UI — including on
  **Alias** objects (e.g. one named like `..._RADIUS_KEY`) that hold a
  secret value, which this script detects generically (any field matching
  Aruba's `vault:v<N>:...` format), not just on the handful of fields it
  already knew about. See "Automating secrets instead of retyping them by
  hand" above for a way to supply the real values up front so this script
  writes them in directly via the API instead — no manual UI step needed.
- **Site/device-group assignment**: as noted above, this script only
  populates the destination's Library — it does not apply any profile to a
  site or device.

## Useful flags

- `--only <list_key1,list_key2,...>` — limit a run to specific object
  types (e.g. `--only role,policy` to just redo roles and policies)
- `--skip-role-repatch` — skip the phase that reattaches policies back
  onto roles (phase F), if you want to handle that by hand instead
- `--export-dir <path>` — where export files are read from/written to
  (default `./export`)
- `--dry-run` — explicit no-op flag; the script is already read-only
  against the destination unless `--apply` is passed
