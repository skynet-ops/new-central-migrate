#!/usr/bin/env python3
"""
new_central_migrate.py
-----------------------
Replicate New Central Library Profiles from a SOURCE tenant to a DESTINATION
tenant, covering every configuration object-type this script could confirm
against a real New Central tenant's API (see CONFIRMATION METHOD below) --
not just WLAN/auth-server/RF/AP-system/NTP/roles/aliases, but also
authentication server groups, policies (ACLs), network groups and network
services (named address/host/FQDN groups and named service/port
definitions, both referenced inside policy rules), DNS, IDS, mesh, ALG,
AAA profiles, gateway system, switch system, AP port profiles, captive
portal, time ranges, and L2 VLANs.

CONFIRMATION METHOD -- read this before you trust a path
==========================================================
Every `list_path` below was hit with a live GET against a real New Central
tenant on 2026-09-11 and either returned real objects or an empty-but-valid
200 (mesh, alg, gw-system). None of these paths are guessed from docs alone.

The create/update shapes below ARE now confirmed live (2026-09-11), via a
create-then-verify-then-delete probe against a real tenant on two object
types (dns, roles), cleaned up immediately after:
  * POST to the bare collection path (e.g. POST /dns, POST /roles) wants
    the SAME envelope GET returns -- {list_key: [...]} -- just with a
    single-element array holding the one new object. NOT a bare object,
    NOT wrapped-but-not-a-list, and NOT wrapped under the URL's own last
    path segment if that differs from list_key (both are wrong and both
    were tried and 400'd first). The exact error that nailed this down:
    "Expecting JSON name/array of objects but list '<list_key>' is
    represented in input data as name/object."
  * PUT to a name-suffixed URL (e.g. PUT /roles/{name} -- an existing,
    specific entry) wants the BARE object with NO envelope at all -- the
    URL already identifies which entry it is. This is the opposite shape
    from the POST-to-collection case; don't "fix" it to match the other.
  * A failed create/update whose response "message" contains "cannot
    change default config" means the object is a protected built-in
    default that already exists in every tenant -- not a real failure,
    see ALREADY_DEFAULT_IN_DEST_MARKERS. Don't be tempted to also match on
    the bare substring "in library" -- a DIFFERENT, unrelated 400 message,
    "Cannot find in library 'X' of type 'Y' referred in 'Z' of type 'W'",
    contains that same substring but means the OPPOSITE thing: a
    dependency the object references is MISSING in the destination, i.e.
    a genuine failure. (This script used to conflate the two -- confirmed
    live 2026-09-11 when it silently mislabeled three real WLAN failures
    as harmless skips. See MISSING_DEPENDENCY_MARKERS /
    is_missing_dependency_error() for the fix and CENTRAL NAC note below.)
  * A THIRD, separate message, "Cannot create duplicate config, Module = X
    where name='Y' already exists in Library", means an object with this
    exact name is already present in the destination -- most commonly
    because a previous run of this script already created it (e.g. you
    re-ran after fixing an earlier, unrelated error further down the
    list). Also a harmless skip, not a real failure -- see
    ALREADY_EXISTS_MARKERS / is_already_exists_error(). Confirmed live
    2026-09-11 on Auth Servers and DNS profiles on a second `--apply` run
    against a destination tenant that already had the first run's objects.
This was confirmed on exactly two object types (a "profile"-shaped one and
a "role"); every other type in OBJECT_TYPES uses the identical create
mechanism per New Central's own docs, so the same shape is applied
uniformly, but if some OTHER type's schema turns out to have its own
quirk (a nested required field, a different key convention, etc.), that
will still surface as a new, type-specific 400 -- report the exact message
and it can be fixed the same way this one was.

A handful of object types are known to exist (they appear in the developer
hub's endpoint list: Auth Server Group, 802.1X auth profile, MAC auth
profile, AP Certificate Usage, Bandwidth Contract) but this script could
not find a working URL slug for 802.1X auth profile, MAC auth profile, AP
Certificate Usage, or Bandwidth Contract after a couple dozen reasonable
guesses (aaa-dot1xauth, dot1x-auth-profile, dot1x, mac-auth-profile,
ap-certificate-usage(s), ap-cert-usage, bandwidth-contract(s), ...all 400).
Auth Server Group WAS found (`server-groups`). The four still-missing types
are referenced BY the objects this script does move (aaa-profile's
"dot1x-auth"/"mac-auth" fields, wlan-ssid's implied bandwidth references)
so this script's reference-checker flags every such reference by name --
you'll get a printed list of exactly what to verify or hand-configure in
the destination tenant because this script can't reach it.

CENTRAL NAC / "cloud-auth" WLANS -- KNOWN LIMITATION
=====================================================
Confirmed live 2026-09-11 against a real destination tenant: any WLAN whose
source JSON has "cloud-auth": true and "primary-auth-server":
"sys_central_nac" (New Central's Central NAC / RADIUS-as-a-Service
integration) is NOT expected to migrate cleanly with this script, for two
observed reasons:
  1. Such WLANs often reference captive-portal / passpoint profiles named
     like "sys_cnac_<wlan-name>" that Central NAC auto-provisions per
     tenant during its own onboarding flow, not ordinary library objects
     that travel with ordinary create calls. Creating the WLAN before that
     onboarding has happened in the destination fails with "Cannot find in
     library '<name>' of type '<type>' referred in '<wlan>' of type
     'aruba-wlan'" -- see MISSING_DEPENDENCY_MARKERS above. The apply loop
     now reports this correctly as an ERR (not a SKIP), but it still can't
     fix it: it prints the error, not a workaround.
  2. Other cloud-auth WLANs fail outright with a validation error --
     "Please Enter Correct value for - basic-rates.: ... vlan-id-range.:
     if auth server group is configured, cannot configure primary
     auth server." -- which looks like a destination-side conflict between
     "primary-auth-server": "sys_central_nac" and an auth server GROUP
     also being implied/required once Central NAC is involved. No working
     request-body change was found for this during the 2026-09-11 session;
     it hasn't been root-caused to a concrete fix yet.
The practical takeaway for both cases: these WLANs likely need Central NAC
onboarded/enabled in the destination tenant first (via its own UI flow, the
same way it originally got created in the source tenant), after which a
retry of this script -- or a manual recreate -- should succeed. This
script deliberately does NOT try to guess or fabricate the missing
Central-NAC-provisioned objects.

IMPORTANT -- "Cannot find in library ... referred in ..." is NOT ALWAYS
Central NAC, and fixing one instance of it does not mean every instance is
fixed. Confirmed live, in order, chasing the SAME "Internet_Only" policy
through TWO successive missing object types:
  1. 2026-09-11: "Cannot find in library 'rfc_1918_private' of type
     'aruba-net-group' referred in 'Internet_Only' of type 'aruba-policy'"
     -- a plain Network Group reference, nothing to do with Central NAC.
     Fixed by adding the "Network groups" entry to OBJECT_TYPES.
  2. 2026-09-12, AFTER that fix, the SAME policy failed again with a
     DIFFERENT missing type: "Cannot find in library 'GoogleNest_TCP' of
     type 'aruba-net-service' referred in 'Internet_Only' of type
     'aruba-policy'" -- a Network Service (named port/protocol
     definition) reference. Fixed by adding the "Network services" entry.
Both were genuinely missing OBJECT TYPES (never migrated by this script
before each was found), not missing individual objects, and both were
found the same way: read the exact "of type '<type>'" in the error,
because that names the actual missing library module -- don't assume
Central NAC just because the message shape matches. is_missing_dependency_
error() catches this message shape generically regardless of which type is
actually missing; if you hit a THIRD "of type 'aruba-<something-new>'" this
script hasn't seen, the fix is the same recipe both times followed: find
its /network-config/v1alpha1/<slug> endpoint (try the plural of the type
name and close variants), pull it, add it to OBJECT_TYPES as an early
phase (A, unless it references something else in scope), done.

ALIASES CAN ALSO HOLD A SOURCE-ENCRYPTED SECRET (NOT JUST AUTH-SERVERS)
========================================================================
Confirmed live 2026-09-13 against a DIFFERENT (second) real tenant: an
Alias object (seen on a RADIUS-shared-secret-style alias, name pattern
like "..._RADIUS_KEY") can itself carry a value encrypted with the SOURCE
tenant's own vault key, the same underlying limitation already documented
below for auth-server "shared-secret-config" -- except this script had no
prior reason to strip anything from Aliases, since most alias types
(ALIAS_ESSID, ALIAS_IPV4_ADDRESS, ALIAS_AUTH_SERVER_ADDRESS,
ALIAS_IDENTITY_PROFILE, ...) hold ordinary, portable values. The failure
is NOT a clean "missing dependency" message -- it comes back as a garbled
400 that dumps a bunch of UNRELATED leaf-list validation noise (radio/RF
fields like "basic-rates", "tx-rates", "vlan-id-range" that have nothing
to do with an Alias) alongside the real problem, which is buried at the
end: "Error finding vault password start from CURL response:
{'errors':['cipher: message authentication failed']}". Don't be misled by
the RF-sounding noise -- that's the destination's generic schema-error
formatter, not a sign this is actually an RF object. See
VAULT_SECRET_MARKERS / is_vault_secret_error() -- this failure is now
reported as its own SKIP category rather than a confusing raw ERR.
IMPORTANT: because this failure can't be predicted from the source data
ahead of time (the encrypted value is opaque; the alias "type" field alone
doesn't reliably say "this holds a secret"), the alias itself is still
attempted and still fails every run -- this is expected, not a bug. Fix:
create that exact alias BY HAND in the destination tenant (same name, same
type) with its real, non-encrypted value (e.g. retype the actual RADIUS
shared secret), then re-run --apply. Because Aliases are phase A's first
entry, this one fix cascades forward automatically: any Authentication
servers that reference this alias by name will then succeed, and any WLAN
SSIDs that reference THOSE auth servers will then succeed in turn on the
same subsequent run -- no separate fix needed for the auth servers or
WLANs themselves, they were only failing because their dependency (this
alias) never got created.

WHY THE PHASES, NOT ONE FLAT LIST
==================================
Objects here reference each other by name, and one pair is flat-out
circular: a Role can carry a list of Policies ("policies": [{"name": ...}])
for enforcement, and a Policy's rules can match traffic by Role
("role-list": ["guest_client", "iot"]) -- confirmed on this tenant's real
"Internet_Only" policy and "BFK" role. Neither can safely be created fully
formed before the other exists. This script breaks the circle the way
Aruba's own tooling does: create Roles WITHOUT their policy attachments
first (phase C), create Policies once Roles exist to be referenced by name
(phase E), then go back and PATCH each Role to reattach its policy list
(phase F). Bandwidth Contracts attached to roles ("aaa-bw-contract") are
NOT part of this script (no working endpoint found) and are stripped
permanently, not reattached -- flagged for manual recreation.

    Phase A  Independent profiles (no refs into anything else in scope):
             auth-servers, dns, ids, mesh, alg, ap-system, gw-system,
             switch-system, ntp, radios (RF), ap-port-profiles,
             time-ranges, l2-vlan
    Phase B  server-groups           (ref: auth-servers)
    Phase C  roles, PASS 1           (policies / aaa-bw-contract stripped)
    Phase D  captive-portal,         (ref: roles, server-groups)
             aaa-profile             (ref: roles, + unresolved dot1x/mac-auth)
    Phase E  policies                (ref: roles, via role-list match conditions)
    Phase F  roles, PASS 2           (PUT to reattach "policies" now that
                                      the Policy objects exist in DEST)
    Phase G  wlan-ssids              (ref: auth-servers, server-groups,
                                      roles, captive-portal -- must be last)

PROTECTED SYSTEM ROLES THAT STILL CARRY CUSTOM POLICIES
=========================================================
A role can be a protected system default (is_system_object() matches it,
e.g. "logon", "recovery", "wired-SetMeUp", "default_wired_port_profile",
"guest-logon", "switch-logon") AND still carry a "policies" list worth
migrating -- some of those roles ship with one or more tenant-editable
policy slots alongside Aruba's built-in SACLs (e.g. "logon" attaches the
built-in "captiveportal"/"ra-guard" ACLs plus the tenant's own
"logon-policy" and "vpnlogon"). Confirmed live 2026-09-11: fully skipping
these roles (the original behavior) silently dropped that attachment --
the Policy objects themselves (e.g. "logon-policy") still got created as
standalone library objects in phase E, they just never got linked back to
their role, so the role in DEST kept only its stock built-in policies.
Fixed: such a role now skips ONLY its phase C create attempt (see
system_protected_role_names) -- since the role object already exists in
every tenant and creating it from scratch 400s -- but is still carried
through to phase F, which attempts a PUT to reattach its policies. Whether
the destination API actually permits a PUT to modify a protected role's
policy list has NOT been independently confirmed as of 2026-09-11 (the
earlier PUT confirmation was on ordinary custom roles only) -- if it 400s,
that's a real answer ("this can't be done via the API, do it in the UI by
hand"), not a bug in this script; report the exact message.
UPDATE 2026-09-13, second tenant: phase F reported "SKIP (already a
protected library default in DEST)" for ALL 16 roles it attempted the PUT
against on this tenant, versus a mix of OK/SKIP seen earlier on the first
tenant/mock testing. This COULD mean this destination tenant/Central
version blocks the policy-reattach PUT for every protected role
outright (a real, hard per-tenant limitation -- reattach those specific
roles' policies by hand in the UI instead), OR it could mean all 16 roles
in that particular run happened to be protected defaults with no ordinary
custom role among them (in which case the result is expected and nothing
is actually stuck). Get one full raw SKIP line (role name + the exact
response message field) to tell the two apart before assuming data loss.

WHAT WON'T COME ACROSS -- READ THIS BEFORE YOU RUN IT
=======================================================
  * Every secret is vault-encrypted with the SOURCE tenant's own vault key
    ("vault:v5:...", "vault:v6:..."), meaningless in the destination. This
    hits auth-server "shared-secret-config" and wlan-ssid
    "personal-security.wpa-passphrase" -- both fields this script knows
    about ahead of time and strips before writing, printed as a checklist
    to retype by hand in the destination UI. It can ALSO hit an Alias
    object whose value itself is a source-encrypted secret (e.g. a
    RADIUS-shared-secret-style alias) -- this one CAN'T be stripped ahead
    of time (the encrypted value is opaque), so it's reported live as a
    SKIP when the create is attempted; see "ALIASES CAN ALSO HOLD A
    SOURCE-ENCRYPTED SECRET" above for the fix.
  * Objects New Central provisions itself (RADIUS-as-a-Service
    "sys_central_nac", "sys_cnac_*" captive-portal/role objects, the
    built-in GW system roles, "DEFAULT_VLAN", etc.) are skipped -- they
    already exist in any tenant. See SYSTEM_OBJECT_MARKERS /
    SYSTEM_OBJECT_EXACT_NAMES to adjust the heuristic.
  * Bandwidth Contracts (role "aaa-bw-contract") and whatever object type
    backs "dot1x-auth" / "mac-auth" / "ap-certificate-usage" references --
    no working API path found for any of these, so they are never created
    and every reference to one is printed in the reference-check report.
  * Anything belonging to a Gateway/SD-WAN persona beyond the bare
    "gw-system" profile (this tenant has none configured, so this script
    was never able to see what a populated one, or a tunnel/uplink/IPsec
    profile, looks like -- those aren't in this script at all).

USAGE
  1. Get an OAuth bearer token for each tenant (your org's usual flow --
     this script doesn't perform the OAuth dance, it consumes two ready
     tokens because token acquisition specifics vary by GreenLake setup).

  2. Set environment variables:
       SRC_BASE_URL   e.g. https://na1.central.arubanetworks.com
       SRC_TOKEN      bearer token for the SOURCE tenant
       DST_BASE_URL   e.g. https://na1.central.arubanetworks.com
       DST_TOKEN      bearer token for the DESTINATION tenant

  3. Dry run first, always:
       python3 new_central_migrate.py --export-dir ./export --dry-run

     Reads everything from SOURCE, writes it to ./export/*.json (backup /
     audit trail), prints the phase-ordered plan, the system-object skip
     list, the secrets checklist, and the full reference-check report.
     Makes ZERO calls against DESTINATION.

  4. Review the export + report. Fix name collisions, confirm the skip
     list and reference-check look right.

  5. Run for real:
       python3 new_central_migrate.py --export-dir ./export --apply

     --only auth-server,role,wlan-ssid limits to specific list_keys (the
     singular key each type's GET response uses, not the label -- see
     the "list_key" in OBJECT_TYPES, or just run without --only once and
     read the printed labels). Handy for re-running just a failed step;
     phase order is still enforced within whatever subset you pick.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
import urllib.parse

# ---------------------------------------------------------------------------
# Every confirmed object type. `phase` controls creation order (A..G, see
# module docstring). `list_key` is the JSON envelope key GET returns the
# array under. `name_field` is the field that identifies an instance --
# almost always "name", but ap-port-profiles uses "profile-name" and
# l2-vlan uses the numeric "vlan" field.
# ---------------------------------------------------------------------------
OBJECT_TYPES = [
    # ---- Phase A: independent, no refs into anything else in this script ----
    # Aliases MUST come first within phase A: apply_phase() creates objects
    # in this list's declared order, and Authentication servers can
    # reference an Alias BY NAME (e.g. a RADIUS shared-secret alias).
    # Confirmed live on a real (different) tenant migration: with Aliases
    # declared AFTER Authentication servers, several auth servers failed
    # with "Cannot find in library 'NAM_RADIUS_KEY' of type 'aruba-alias'
    # referred in '<auth-server>' of type 'aruba-auth-server'" -- the alias
    # genuinely existed in the source data and got migrated fine, just too
    # late in this same phase to exist yet when the auth server referencing
    # it was created. Moving Aliases to the front of phase A fixes this for
    # every tenant, not just the one that surfaced it.
    {"phase": "A", "label": "Aliases", "list_path": "/network-config/v1alpha1/aliases",
     "list_key": "alias", "name_field": "name"},
    {"phase": "A", "label": "Authentication servers", "list_path": "/network-config/v1alpha1/auth-servers",
     "list_key": "auth-server", "name_field": "name", "secret_fields": ["shared-secret-config"]},
    {"phase": "A", "label": "DNS profiles", "list_path": "/network-config/v1alpha1/dns",
     "list_key": "profile", "name_field": "name"},
    {"phase": "A", "label": "IDS profiles", "list_path": "/network-config/v1alpha1/ids",
     "list_key": "profile", "name_field": "name"},
    {"phase": "A", "label": "Mesh profiles", "list_path": "/network-config/v1alpha1/mesh",
     "list_key": "profile", "name_field": "name"},
    {"phase": "A", "label": "ALG profiles", "list_path": "/network-config/v1alpha1/alg",
     "list_key": "profile", "name_field": "name"},
    {"phase": "A", "label": "AP system profile", "list_path": "/network-config/v1alpha1/ap-system",
     "list_key": "profile", "name_field": "name"},
    {"phase": "A", "label": "Gateway system profile", "list_path": "/network-config/v1alpha1/gw-system",
     "list_key": "profile", "name_field": "name"},
    {"phase": "A", "label": "Switch system profiles", "list_path": "/network-config/v1alpha1/switch-system",
     "list_key": "profile", "name_field": "name"},
    {"phase": "A", "label": "NTP profiles", "list_path": "/network-config/v1alpha1/ntp",
     "list_key": "profile", "name_field": "name"},
    {"phase": "A", "label": "RF profiles (radios)", "list_path": "/network-config/v1alpha1/radios",
     "list_key": "profile", "name_field": "name"},
    {"phase": "A", "label": "AP port profiles", "list_path": "/network-config/v1alpha1/ap-port-profiles",
     "list_key": "profile", "name_field": "profile-name"},
    {"phase": "A", "label": "Time ranges", "list_path": "/network-config/v1alpha1/time-ranges",
     "list_key": "time-range", "name_field": "name"},
    {"phase": "A", "label": "L2 VLANs", "list_path": "/network-config/v1alpha1/l2-vlan",
     "list_key": "l2-vlan", "name_field": "vlan", "display_field": "name"},
    # Network Groups (internal type "aruba-net-group") -- named address/host/
    # FQDN groups referenced BY NAME inside Policy (ACL) rule conditions
    # (e.g. a rule's condition.destination might say "net-group":
    # "rfc_1918_private"). Discovered live 2026-09-11 as a MISSING OBJECT
    # TYPE, not just a missing individual object: a real Policy named
    # "Internet_Only" failed to create in a destination tenant with "Cannot
    # find in library 'rfc_1918_private' of type 'aruba-net-group' referred
    # in 'Internet_Only' of type 'aruba-policy'" -- this script had never
    # migrated net-groups at all before this, because Policies (phase E) was
    # believed to only reference roles. Endpoint found by trying reasonable
    # candidate paths against a real tenant: /net-groups was the only one
    # that returned 200 (the others -- network-groups, netgroups,
    # net-destinations, network-destinations, destination-groups,
    # address-groups, netdst-groups -- all 400'd as unrecognized module
    # names). Must be created before Policies (phase E), hence phase A.
    {"phase": "A", "label": "Network groups", "list_path": "/network-config/v1alpha1/net-groups",
     "list_key": "net-group", "name_field": "name"},
    # Network Services (internal type "aruba-net-service") -- named service/
    # port definitions (protocol + port or port-range, or a bare IANA
    # protocol number) referenced BY NAME inside Policy (ACL) rule
    # conditions as "net-service": "<name>". Discovered live 2026-09-12
    # the exact same way as net-groups: after net-groups was added,
    # "Internet_Only" still failed to create, this time with "Cannot find
    # in library 'GoogleNest_TCP' of type 'aruba-net-service' referred in
    # 'Internet_Only' of type 'aruba-policy'". Most of the 78 entries on a
    # real tenant are Aruba's own built-in "svc-*" definitions (svc-dhcp,
    # svc-https, etc., which already exist in every tenant and will report
    # as SKIP via is_already_default_error/is_already_exists_error, same as
    # any other protected default) alongside a handful of genuinely custom
    # ones (e.g. "GoogleNest_TCP", "Nintendo_Switch_TCP", "RingDoorbell_TCP",
    # "tcp80") that actually need migrating. Endpoint found the same way as
    # net-groups: /net-services returned 200 immediately. Must be created
    # before Policies (phase E), hence phase A.
    {"phase": "A", "label": "Network services", "list_path": "/network-config/v1alpha1/net-services",
     "list_key": "net-service", "name_field": "name"},

    # ---- Phase B: depends on auth-servers ----
    {"phase": "B", "label": "Authentication server groups", "list_path": "/network-config/v1alpha1/server-groups",
     "list_key": "server-group", "name_field": "name",
     # "servers-count" is a server-computed, read-only field (a count of
     # entries in "servers") that GET returns but POST rejects. Confirmed
     # live 2026-09-11 against a real destination tenant: sending it back
     # verbatim 400's with "failed to parse data tree: Node 'servers-count'
     # not found as a child of 'server-group' node." -- the same class of
     # bug as a stray read-only field, not a schema/shape problem. Every
     # other exported object type was checked for a similar "*-count"-style
     # computed field on 2026-09-11 and none were found, so this strip is
     # scoped to server-groups only for now.
     "always_strip": ["servers-count"]},

    # ---- Phase C: roles, pass 1 (bare -- policies / bw-contract stripped) ----
    {"phase": "C", "label": "Roles", "list_path": "/network-config/v1alpha1/roles",
     "list_key": "role", "name_field": "name",
     "strip_for_pass1": ["policies", "aaa-bw-contract"]},

    # ---- Phase D: depends on roles + phase A/B ----
    {"phase": "D", "label": "Captive portal profiles", "list_path": "/network-config/v1alpha1/captive-portal",
     "list_key": "profile", "name_field": "name"},
    {"phase": "D", "label": "AAA profiles", "list_path": "/network-config/v1alpha1/aaa-profile",
     "list_key": "profile", "name_field": "name"},

    # ---- Phase E: depends on roles (policy rules match traffic by role) ----
    {"phase": "E", "label": "Policies (ACLs)", "list_path": "/network-config/v1alpha1/policies",
     "list_key": "policy", "name_field": "name"},

    # ---- Phase F: roles, pass 2 -- handled specially in main(), not looped generically ----

    # ---- Phase G: must be last -- references almost everything above ----
    {"phase": "G", "label": "WLAN SSIDs", "list_path": "/network-config/v1alpha1/wlan-ssids",
     "list_key": "wlan-ssid", "name_field": "ssid",
     "secret_fields": ["personal-security.wpa-passphrase"]},
]

PHASE_ORDER = ["A", "B", "C", "D", "E", "F", "G"]

# Any key name, found anywhere in an object (however deeply nested), that
# names another object by value. Used only for the reference-check report --
# it never blocks a write, it just tells you what to go verify.
STRING_REFERENCE_KEYS = {
    "primary-auth-server": "Authentication server or server group",
    "backup-auth-server": "Authentication server or server group",
    "primary-acct-server": "Authentication server or server group",
    "backup-acct-server": "Authentication server or server group",
    "server-group": "Authentication server group",
    "default-role": "Role",
    "pre-auth-role": "Role",
    "dot1x-default-role": "Role",
    "mac-default-role": "Role",
    "captive-portal": "Captive portal profile",
    "dot1x-auth": "802.1X auth profile (NOT covered -- no working API path found)",
    "mac-auth": "MAC auth profile (NOT covered -- no working API path found)",
}
# key whose value is a list of plain strings
LIST_OF_STRING_REFERENCE_KEYS = {
    "role-list": "Role",
}
# key whose value is a list of dicts -- (field to read the name from, label)
LIST_OF_DICT_REFERENCE_KEYS = {
    "policies": ("name", "Policy (ACL)"),
    "servers": ("server-name", "Authentication server"),
}
# key whose value is a nested structure we don't fully model -- just note
# that *something* under here references an out-of-scope object type
FLAG_ONLY_KEYS = {
    "aaa-bw-contract": "Bandwidth Contract (NOT covered -- no working API path found)",
}

SYSTEM_OBJECT_MARKERS = (
    "system generated configuration",
    "system defined role",
    "system role",
    "do not edit or delete",
    "wlan access-rule",  # confirmed live 2026-09-11: e.g. role "logon" (desc "wlan
                         # access-rule logon") 400'd on create with "Cannot change
                         # default config (aruba-role/logon) in library." -- this is
                         # a SECOND, separate description convention Aruba uses for
                         # protected built-in library roles/policies, distinct from
                         # the "system defined role for GW" convention already
                         # covered above. Confirmed to also cover "recovery",
                         # "wired-SetMeUp", "default_wired_port_profile".
    "not editable",      # confirmed live 2026-09-11: net-group "private-networks"
                         # (desc "System defined net destination as per RFC1918.
                         # Not Editable.") -- a THIRD description convention,
                         # this one seen on Network Groups rather than roles.
                         # Distinct from the very similarly-named custom
                         # net-group "rfc_1918_private" (no system marker in its
                         # description), which is NOT a protected default and
                         # does need to be migrated -- don't confuse the two.
)
SYSTEM_OBJECT_NAME_PREFIXES = ("sys_", "sys-")
SYSTEM_OBJECT_EXACT_NAMES = {
    "DEFAULT_VLAN", "default",
    # Confirmed live 2026-09-13: this Policy has an ordinary-looking name and
    # description (nothing in SYSTEM_OBJECT_MARKERS matches it), so it sailed
    # past this heuristic, through a create attempt (400 "already exists in
    # Library" -- matches is_already_exists_error, NOT is_already_default_
    # error), and then through this script's own refresh-PUT, which is what
    # finally revealed its true nature: HTTP 400 "Validation failure: The
    # policy name System Default GW Policy is restricted." That's a FOURTH,
    # distinct wording Aruba uses for "this is a protected built-in", seen
    # only on the update path rather than the create path this time. No data
    # was ever at risk (both the create and the PUT were rejected by the
    # server itself before anything could be overwritten), but this name is
    # now listed here so future runs recognize it up front and skip cleanly
    # ("SKIP (system-generated)") instead of spending two wasted API calls
    # to rediscover the same thing.
    "System Default GW Policy",
}

# Substrings seen in a live 400 response body's "message" field that mean
# "this object is a protected built-in library default that already exists
# in every tenant" rather than a genuine failure. Confirmed live 2026-09-11
# via error: "Cannot change default config (aruba-role/logon) in library."
# When a create/update call fails with one of these, the apply loop treats
# it as an informational skip (the object is already there) instead of an
# ERR needing investigation. This exists because SYSTEM_OBJECT_MARKERS can't
# catch every protected default up front -- some ship with ordinary-looking
# descriptions this heuristic has no way to distinguish from real customer
# objects until the destination tenant itself says so.
#
# NOTE: this used to also include the bare substring "in library", on the
# assumption every "...in library" message meant an already-exists skip.
# That was wrong -- confirmed live 2026-09-11 against a real destination
# tenant, a WLAN create can also fail with "Cannot find in library 'X' of
# type 'Y' referred in 'Z' of type 'W'", which means a REFERENCED DEPENDENCY
# IS MISSING in the destination (a genuine failure), not that the object
# already exists. Both messages contain "in library", so the old broad
# marker silently mislabeled real failures as harmless SKIPs. Only the
# specific, unambiguous phrase is kept here; the "cannot find in library"
# case is handled separately by is_missing_dependency_error() below.
ALREADY_DEFAULT_IN_DEST_MARKERS = (
    "cannot change default config",
)

# Substrings seen in a live 400 response body's "message" field that mean
# "this object references another library object (by name) that does not
# exist in the destination tenant" -- a genuine failure, not a skip.
# Confirmed live 2026-09-11 against a real destination tenant, e.g.:
#   "Cannot find in library 'sys_cnac_bfk-guest' of type
#    'aruba-aaa-captive-portal' referred in 'BFK-Guest' of type 'aruba-wlan'"
# This happens for WLANs that rely on New Central's Central NAC / RADIUS-
# as-a-Service integration ("cloud-auth": true, "primary-auth-server":
# "sys_central_nac"): the referenced captive-portal / passpoint profiles
# are auto-provisioned per-tenant by Central NAC onboarding, not ordinary
# library objects that travel with a normal export/import.
MISSING_DEPENDENCY_MARKERS = (
    "cannot find in library",
)

# Substrings seen in a live 400 response body's "message" field that mean
# "an object with this exact name already exists in the destination
# tenant's library" -- NOT a protected built-in default (that's
# ALREADY_DEFAULT_IN_DEST_MARKERS above), just an ordinary object that's
# already there, most commonly because a previous run of this script
# already created it (e.g. a retry after fixing an unrelated error further
# down the object list). Confirmed live 2026-09-11 against a real
# destination tenant, e.g.:
#   "Cannot create duplicate config, Module = Auth Server where
#    name='CPPM_VIP_Ash' already exists in Library"
# Safe to treat as an informational skip: the object is present either
# way, so there's nothing to create. This does NOT verify the existing
# object's fields match the source -- if you changed the source object
# since the tenant's last apply run, the destination copy could be stale;
# rerun with a fresh export if that matters.
ALREADY_EXISTS_MARKERS = (
    "cannot create duplicate config",
    "already exists in library",
)

# Substrings seen in a live 400 response body's "message" field that mean
# "this object's value was encrypted with the SOURCE tenant's own vault key,
# and the DESTINATION tenant cannot decrypt it" -- a genuine, unfixable-by-
# this-script failure, but a DIFFERENT one from a missing dependency.
# Confirmed live 2026-09-13 against a real destination tenant on an Alias
# object (name pattern like "..._RADIUS_KEY"), e.g.:
#   "failed to parse data tree: Predicate missing for leaf-list
#    'basic-rates' in path.: ... Error finding vault password start from
#    CURL response: {'errors':['cipher: message authentication failed']}"
# The "Predicate missing for leaf-list ..." noise earlier in the same
# message is the destination's generic schema-validation error formatter
# dumping unrelated leaf complaints (radio/RF fields that have nothing to
# do with an Alias) alongside the real problem; don't be misled into
# thinking this is an RF/radio object -- the substantive failure is the
# "vault password" / "cipher: message authentication failed" part.
# This is the SAME underlying limitation already documented for
# auth-server "shared-secret-config" (see strip_secrets/secret_fields
# above) -- it just also applies to certain Alias objects (e.g. type
# ALIAS_SHARED_SECRET-style aliases used to hold a RADIUS shared secret by
# reference) that this script has no generic way to detect ahead of time,
# since the encrypted value is opaque and the alias "type" field alone
# doesn't reliably say "this holds a secret". Reported as a distinct SKIP
# (not a generic ERR) so it reads as "expected and actionable" rather than
# a mystery failure: create this exact alias by hand in the destination
# with the real secret value, then re-run --apply (safe/idempotent) to
# pick up everything that referenced it (auth servers, then any WLANs that
# reference those auth servers).
VAULT_SECRET_MARKERS = (
    "vault password",
    "cipher: message authentication failed",
)


# ---------------------------------------------------------------------------
# Tiny HTTP helper (stdlib only)
# ---------------------------------------------------------------------------
def api_call(base_url, token, method, path, body=None, _max_retries=2):
    """
    Confirmed live 2026-09-13: a plain network hiccup (a single request
    timing out) used to crash this ENTIRE script -- urllib raises
    urllib.error.URLError for a connect/read timeout, which this function
    previously did not catch at all (only urllib.error.HTTPError, a
    completed-but-non-2xx response, was handled). A migration run makes
    hundreds of sequential calls; on a real network, one of them timing
    out sooner or later isn't exotic, and it was taking down every object
    type still queued behind it (confirmed: a run died mid "[Phase G] WLAN
    SSIDs" after 1 of 8 WLANs, with 7 never attempted at all).

    Fixed two ways: (1) a network-level failure (timeout, DNS failure,
    connection reset, etc.) is retried up to `_max_retries` times with a
    short backoff, since these are often transient; (2) if it still fails
    after retries, this returns a synthetic (0, {"message": ...}) instead
    of raising, so the caller's normal is_*_error()/ERR handling takes
    over and the loop moves on to the NEXT object instead of dying. Rerun
    with --apply afterward to pick up anything that got an ERR this way --
    it's safe, everything already created is skipped as already-existing.
    """
    url = base_url.rstrip("/") + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    attempt = 0
    while True:
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                if not raw:
                    return resp.status, {}
                try:
                    return resp.status, json.loads(raw)
                except json.JSONDecodeError:
                    # A 2xx with a body that isn't valid JSON is unexpected
                    # but not worth crashing the whole run over either --
                    # same defensive spirit as the network-error handling
                    # below. Surface the raw text so it's still visible.
                    return resp.status, {"raw": raw.decode("utf-8", "replace")}
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = {"raw": raw.decode("utf-8", "replace")}
            return e.code, parsed
        except (urllib.error.URLError, OSError) as e:
            attempt += 1
            if attempt > _max_retries:
                return 0, {"message": f"Network error calling {method} {path}: {e}",
                           "network_error": True}
            time.sleep(2 * attempt)
        except Exception as e:
            # Confirmed live 2026-09-13: an object name containing a space
            # (a real, legitimate Aruba object name -- e.g. an Alias named
            # "ashburn cppm guest appliance") raised http.client.InvalidURL
            # ("URL can't contain control characters") when building a
            # name-suffixed PUT URL without percent-encoding it -- a
            # DIFFERENT exception type than the network-timeout case above,
            # so it slipped straight past that fix and crashed the whole
            # run again. Fixed at the root by percent-encoding every name
            # used in a URL (see urllib.parse.quote at both PUT call
            # sites), but this catch-all stays as a second line of
            # defense: whatever the actual exception type future data
            # triggers, this function's job is to always return a
            # (status, body) tuple, never let one object's problem take
            # down every other object queued behind it. Not retried (most
            # exceptions here are deterministic -- retrying the exact same
            # bad input just fails again identically), just reported.
            return 0, {"message": f"Unexpected error calling {method} {path}: "
                                   f"{type(e).__name__}: {e}",
                       "network_error": True}


def get_name(obj, otype):
    val = obj.get(otype["name_field"])
    if otype.get("display_field") and obj.get(otype["display_field"]):
        return f"{obj[otype['display_field']]} (vlan {val})"
    return val if val is not None else "<unnamed>"


def is_system_object(obj, otype=None):
    desc = (obj.get("description") or "").lower()
    if any(marker in desc for marker in SYSTEM_OBJECT_MARKERS):
        return True
    name = obj.get("name") or obj.get("ssid") or obj.get("profile-name")
    if name and (name.lower().startswith(SYSTEM_OBJECT_NAME_PREFIXES) or name in SYSTEM_OBJECT_EXACT_NAMES):
        return True
    if otype and otype.get("name_field") == "vlan" and obj.get("name") in SYSTEM_OBJECT_EXACT_NAMES:
        return True
    return False


def get_nested(obj, dotted_field):
    cur = obj
    for part in dotted_field.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def set_nested(obj, dotted_field, value):
    parts = dotted_field.split(".")
    cur = obj
    for part in parts[:-1]:
        if not isinstance(cur, dict) or part not in cur:
            return
        cur = cur[part]
    if isinstance(cur, dict) and parts[-1] in cur:
        cur[parts[-1]] = value


def strip_secrets(obj, secret_fields):
    """Return (cleaned_copy, list_of_secret_field_paths_removed)."""
    cleaned = json.loads(json.dumps(obj))
    removed = []
    for field in secret_fields or []:
        if get_nested(cleaned, field) is not None:
            removed.append(field)
            set_nested(cleaned, field, None)
    return cleaned, removed


# Aruba's own convention for a vault-encrypted value, confirmed live on
# auth-server "shared-secret-config" and wlan-ssid "personal-security.wpa-
# passphrase": a string starting "vault:v<N>:...". This is what let
# NAM_RADIUS_KEY (an Alias -- an object type this script never declared any
# secret_fields for) get caught generically instead of only being
# discovered the hard way via a "cipher: message authentication failed"
# 400 at apply time -- see VAULT_SECRET_MARKERS / is_vault_secret_error()
# for that reactive fallback, which stays in place as a safety net for
# anything this proactive scan misses.
VAULT_STRING_RE = re.compile(r"^vault:v\d+:", re.IGNORECASE)


def find_vault_strings(obj, path=""):
    """Recursively find every string value anywhere in obj that looks like
    a vault-encrypted secret (see VAULT_STRING_RE), regardless of whether
    this script already has that field listed in an OBJECT_TYPES entry's
    secret_fields. Returns [(dotted_path, current_value), ...]."""
    findings = []
    if isinstance(obj, dict):
        for key, val in obj.items():
            here = f"{path}.{key}" if path else key
            if isinstance(val, str) and VAULT_STRING_RE.match(val):
                findings.append((here, val))
            else:
                findings.extend(find_vault_strings(val, here))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            findings.extend(find_vault_strings(item, f"{path}[{i}]"))
    return findings


def resolve_secret_override(list_key, name, secrets_overrides):
    """Look up a user-supplied real secret value for this object (see
    --secrets-file). Keyed "<list_key>:<name>" -- e.g. "alias:NAM_RADIUS_KEY".
    The value can be a plain string (used for the object's one secret field)
    or, for an object with more than one secret field, a dict of
    {dotted_path: real_value}."""
    if not secrets_overrides:
        return None
    return secrets_overrides.get(f"{list_key}:{name}")


def apply_secrets(obj, otype, name, secrets_overrides):
    """
    Combined replacement for the old bare strip_secrets() call: finds every
    secret field in obj -- both the ones this OBJECT_TYPES entry already
    knows about (otype["secret_fields"]) and any OTHER field anywhere in
    the object that looks vault-encrypted (find_vault_strings) -- and for
    each one either substitutes the real value from --secrets-file (fully
    automated, no manual UI step needed) or nulls it out for manual retyping,
    exactly as strip_secrets did before this existed.

    Returns (cleaned_copy, actions) where actions is a list of
    (dotted_path, "provided" | "manual") describing what happened to each
    secret field found.
    """
    cleaned = json.loads(json.dumps(obj))
    found_paths = set(otype.get("secret_fields", []) or [])
    for path, _value in find_vault_strings(cleaned):
        found_paths.add(path)

    actions = []
    if not found_paths:
        return cleaned, actions

    override = resolve_secret_override(otype["list_key"], name, secrets_overrides)
    single_field = len(found_paths) == 1

    for path in sorted(found_paths):
        if get_nested(cleaned, path) is None:
            continue  # field not present on this particular object -- nothing to do
        real_value = None
        if isinstance(override, dict):
            real_value = override.get(path)
        elif override is not None and single_field:
            real_value = override
        if real_value is not None:
            set_nested(cleaned, path, real_value)
            actions.append((path, "provided"))
        else:
            set_nested(cleaned, path, None)
            actions.append((path, "manual"))
    return cleaned, actions


def load_secrets_file(path):
    """Load the --secrets-file JSON mapping. Empty/absent path -> {}."""
    if not path:
        return {}
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except FileNotFoundError:
        sys.exit(f"--secrets-file {path} not found.")
    except json.JSONDecodeError as e:
        sys.exit(f"--secrets-file {path} is not valid JSON: {e}")
    if not isinstance(data, dict):
        sys.exit(f"--secrets-file {path} must contain a JSON object mapping "
                  f"\"<list_key>:<name>\" to a real secret value (or a "
                  f"{{dotted_path: value}} object for multi-field cases).")
    return data


def strip_fields(obj, fields):
    """Return (cleaned_copy, {field: original_value} for every field that was present)."""
    cleaned = json.loads(json.dumps(obj))
    removed = {}
    for field in fields or []:
        if field in cleaned:
            removed[field] = cleaned.pop(field)
    return cleaned, removed


def scan_references(obj, path=""):
    """
    Recursively walk `obj`. Returns a list of (path, label, referenced_name)
    for every reference this script knows how to recognize -- see the three
    REFERENCE_KEYS dicts and FLAG_ONLY_KEYS above.
    """
    findings = []
    if isinstance(obj, dict):
        for key, val in obj.items():
            here = f"{path}.{key}" if path else key
            if key in STRING_REFERENCE_KEYS and isinstance(val, str) and val:
                findings.append((here, STRING_REFERENCE_KEYS[key], val))
            elif key in LIST_OF_STRING_REFERENCE_KEYS and isinstance(val, list):
                for name in val:
                    if isinstance(name, str) and name:
                        findings.append((here, LIST_OF_STRING_REFERENCE_KEYS[key], name))
            elif key in LIST_OF_DICT_REFERENCE_KEYS and isinstance(val, list):
                name_field, label = LIST_OF_DICT_REFERENCE_KEYS[key]
                for item in val:
                    if isinstance(item, dict) and item.get(name_field):
                        findings.append((here, label, item[name_field]))
            elif key in FLAG_ONLY_KEYS and val:
                findings.append((here, FLAG_ONLY_KEYS[key], "<see object -- structure not fully modeled>"))
            else:
                findings.extend(scan_references(val, here))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            findings.extend(scan_references(item, f"{path}[{i}]"))
    return findings


def export_slug(otype):
    """
    A unique-per-object-type filename stem. NOTE: list_key is NOT unique --
    DNS/IDS/Mesh/ALG/AP-system/GW-system/Switch-system/NTP/Captive-portal/AAA
    profiles all share the JSON envelope key "profile", so using list_key
    here would make them all collide on one profile.json and silently
    overwrite each other. list_path is unique per entry in OBJECT_TYPES, so
    its last segment is used instead.
    """
    return otype["list_path"].rstrip("/").split("/")[-1]


def is_already_default_error(resp):
    """True if a failed create/update's response body says the object is a
    protected built-in library default that already exists in the
    destination tenant (see ALREADY_DEFAULT_IN_DEST_MARKERS) -- i.e. this
    isn't a real failure, the object just doesn't need to be created."""
    if not isinstance(resp, dict):
        return False
    msg = str(resp.get("message", "")).lower()
    return any(marker in msg for marker in ALREADY_DEFAULT_IN_DEST_MARKERS)


def is_missing_dependency_error(resp):
    """True if a failed create/update's response body says the object
    references another library object (by name) that doesn't exist in the
    destination tenant (see MISSING_DEPENDENCY_MARKERS) -- a genuine
    failure, NOT an already-exists skip, even though the message also
    happens to contain the words "in library"."""
    if not isinstance(resp, dict):
        return False
    msg = str(resp.get("message", "")).lower()
    return any(marker in msg for marker in MISSING_DEPENDENCY_MARKERS)


def is_already_exists_error(resp):
    """True if a failed create's response body says an object with this
    exact name already exists in the destination tenant's library (see
    ALREADY_EXISTS_MARKERS) -- e.g. left over from a previous run of this
    script. Not a protected built-in default (is_already_default_error)
    and not a missing dependency (is_missing_dependency_error); just an
    ordinary object that doesn't need to be created again."""
    if not isinstance(resp, dict):
        return False
    msg = str(resp.get("message", "")).lower()
    return any(marker in msg for marker in ALREADY_EXISTS_MARKERS)


def is_vault_secret_error(resp):
    """True if a failed create/update's response body says the object's
    value was encrypted with the SOURCE tenant's own vault key and can't be
    decrypted in the DESTINATION (see VAULT_SECRET_MARKERS) -- a genuine,
    unfixable-by-this-script failure that needs the object recreated by
    hand in DEST with its real (non-encrypted-from-source) value."""
    if not isinstance(resp, dict):
        return False
    msg = str(resp.get("message", "")).lower()
    return any(marker in msg for marker in VAULT_SECRET_MARKERS)


def save_export_file(export_dir, slug, items):
    os.makedirs(export_dir, exist_ok=True)
    path = os.path.join(export_dir, f"{slug}.json")
    with open(path, "w") as f:
        json.dump(items, f, indent=2)
    return path


def load_export_file(export_dir, slug):
    path = os.path.join(export_dir, f"{slug}.json")
    with open(path) as f:
        return json.load(f), path


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--export-dir", default="./export", help="Where source objects get saved as JSON")
    ap.add_argument("--only", help="Comma-separated list_key values to limit to, e.g. wlan-ssid,role")
    ap.add_argument("--apply", action="store_true", help="Actually create objects in DESTINATION")
    ap.add_argument("--dry-run", action="store_true",
                     help="No-op / explicit marker only -- dry run is already what happens "
                          "whenever --apply is not passed. Accepted so the usage examples in "
                          "this script's own docstring work verbatim.")
    ap.add_argument("--skip-role-repatch", action="store_true",
                     help="Don't attempt phase F (PUT to reattach role->policy links). "
                          "Use this if your tenant's update route differs from the "
                          "inferred {collection}/{name} PUT and phase F errors out.")
    ap.add_argument("--no-refresh-existing", action="store_true",
                     help="Don't attempt to PUT fresh content into an object that already "
                          "exists in DEST because an earlier run of this script created it "
                          "(the \"already exists...likely from a prior run\" SKIP). By "
                          "default this script tries to refresh such objects, since an "
                          "earlier partial run may have created one before a dependency it "
                          "referenced (e.g. a Role named in a policy rule) existed yet, "
                          "leaving stale content behind that a plain create-or-skip would "
                          "otherwise never fix. This NEVER touches a genuine protected "
                          "library default (\"cannot change default config\") -- only "
                          "objects this script itself is responsible for.")
    ap.add_argument("--from-export", action="store_true",
                     help="Skip reading SOURCE over the network entirely -- load "
                          "{export-dir}/{list_key}.json files that were already saved by a "
                          "prior run instead. SRC_BASE_URL/SRC_TOKEN are not required in this "
                          "mode. Useful for re-running --apply after a partial failure without "
                          "re-reading SOURCE, or when SOURCE was already read by something else "
                          "and only DESTINATION needs live credentials.")
    ap.add_argument("--secrets-file",
                     help="Path to a local JSON file supplying REAL values for secrets that "
                          "can't travel from SOURCE (vault-encrypted shared secrets, WPA "
                          "passphrases, secret-holding Aliases like a RADIUS key). When a "
                          "value is supplied here, this script writes it directly via the "
                          "API instead of stripping the field and asking you to retype it "
                          "by hand in the destination UI. Format: a JSON object keyed "
                          "\"<list_key>:<name>\" (e.g. \"alias:NAM_RADIUS_KEY\": \"the real "
                          "secret\", \"auth-server:US_RADIUS_CHI_VIP1\": \"the real shared "
                          "secret\"). This file holds real secrets -- keep it out of version "
                          "control and delete it when you're done; it is never written "
                          "anywhere by this script.")
    args = ap.parse_args()

    src_base = os.environ.get("SRC_BASE_URL")
    src_token = os.environ.get("SRC_TOKEN")
    dst_base = os.environ.get("DST_BASE_URL")
    dst_token = os.environ.get("DST_TOKEN")

    if not args.from_export and (not src_base or not src_token):
        sys.exit("Set SRC_BASE_URL and SRC_TOKEN environment variables (source tenant), "
                  "or pass --from-export to load previously saved export/*.json files instead.")
    if args.apply and (not dst_base or not dst_token):
        sys.exit("Set DST_BASE_URL and DST_TOKEN environment variables (destination tenant) before --apply.")

    only = set(x.strip() for x in args.only.split(",")) if args.only else None
    types = [t for t in OBJECT_TYPES if not only or t["list_key"] in only]
    secrets_overrides = load_secrets_file(args.secrets_file)

    print("=" * 78)
    print(f"SOURCE: {'(loaded from ' + args.export_dir + ' via --from-export)' if args.from_export else src_base}")
    if args.apply:
        print(f"DEST:   {dst_base}")
    print(f"Mode:   {'APPLY (writes to destination)' if args.apply else 'DRY RUN (read-only)'}")
    print(f"Types:  {len(types)} object types across phases "
          f"{sorted(set(t['phase'] for t in types))}")
    print("=" * 78)

    # role_original[name] = the untouched source role object, kept aside so
    # phase F can PUT the real "policies" list back in once Policies exist.
    role_original = {}
    # Names of roles that are protected system defaults (is_system_object()
    # matched them) but still carry a custom policy attachment worth
    # preserving -- e.g. "logon" ships with Aruba's built-in SACLs PLUS one
    # tenant-editable slot ("logon-policy"/"vpnlogon"), same for "recovery",
    # "wired-SetMeUp", "default_wired_port_profile". These roles are kept in
    # the pipeline so phase F can still attempt to reattach their policies,
    # but phase C must NOT try to create them from scratch (that already
    # 400s with "Cannot change default config"; see
    # ALREADY_DEFAULT_IN_DEST_MARKERS) -- this set is how apply_phase()
    # knows to skip the create attempt for exactly these roles.
    system_protected_role_names = set()
    all_cleaned = {}      # export_slug(otype) -> [(cleaned_obj_for_create, raw_obj), ...]
                           # NOTE: keyed by slug, NOT list_key -- list_key is not unique
                           # (a dozen types share the envelope key "profile"), so keying
                           # this by list_key would silently overwrite and drop most of
                           # them before apply_phase() ever ran. Learn from this.
    all_findings = []     # (otype_label, obj_name, ref_path, ref_label, ref_name)

    # ---- Phase 1 of the SCRIPT (not to be confused with object phases): ----
    # read everything from SOURCE, save it, classify it, strip what must be
    # stripped, and run the reference scanner.
    for otype in types:
        slug = export_slug(otype)
        if args.from_export:
            try:
                items, path = load_export_file(args.export_dir, slug)
            except FileNotFoundError:
                print(f"\n[FAIL] --from-export: no saved file for {otype['label']} at "
                      f"{args.export_dir}/{slug}.json")
                all_cleaned[slug] = []
                continue
            print(f"\n{otype['label']} [phase {otype['phase']}]: {len(items)} loaded from {path}")
        else:
            status, body = api_call(src_base, src_token, "GET", otype["list_path"])
            if status != 200:
                print(f"\n[FAIL] GET {otype['list_path']} -> {status}: {body}")
                all_cleaned[slug] = []
                continue
            items = body.get(otype["list_key"], [])
            path = save_export_file(args.export_dir, slug, items)
            print(f"\n{otype['label']} [phase {otype['phase']}]: {len(items)} found, saved to {path}")

        keep_list = []
        for obj in items:
            name = get_name(obj, otype)
            is_role = otype["list_key"] == "role"
            system_flag = is_system_object(obj, otype)
            # A protected system role (e.g. "logon", "recovery",
            # "wired-SetMeUp", "default_wired_port_profile") that ALSO
            # carries its own "policies" list is not fully dropped: its
            # base role object already exists in every tenant (phase C
            # create is skipped for it, see system_protected_role_names
            # below), but its custom policy attachment is real per-tenant
            # data. Confirmed live 2026-09-11: skipping these roles
            # entirely (the old behavior) silently dropped that
            # attachment -- the Policy objects themselves (e.g.
            # "logon-policy", "vpnlogon") still got created standalone in
            # phase E, just never linked to their role. Every OTHER
            # system-flagged object (and system roles with no custom
            # policies) is still skipped outright as before.
            protected_role_with_policies = system_flag and is_role and "policies" in obj
            if system_flag and not protected_role_with_policies:
                print(f"  - SKIP (system-generated): {name}")
                continue

            working = obj
            if is_role:
                role_original[obj.get("name")] = obj
                working, stripped = strip_fields(obj, otype.get("strip_for_pass1", []))
                if protected_role_with_policies:
                    system_protected_role_names.add(obj.get("name"))

            # Server-computed / read-only fields that GET returns but a
            # create/update rejects (e.g. server-groups' "servers-count") --
            # see the "always_strip" comment on that OBJECT_TYPES entry.
            working, _ = strip_fields(working, otype.get("always_strip", []))

            cleaned, secret_actions = apply_secrets(working, otype, name, secrets_overrides)
            removed_secrets = [p for p, how in secret_actions if how == "manual"]
            provided_secrets = [p for p, how in secret_actions if how == "provided"]
            if protected_role_with_policies:
                print(f"  - {name}: system-protected role (already exists in every tenant) -- "
                      f"skipping create, but will still attempt to reattach its policies in phase F")
            elif provided_secrets and removed_secrets:
                print(f"  - {name}: will migrate; secret field(s) filled from --secrets-file: "
                      f"{', '.join(provided_secrets)}; still needs manual retype: {', '.join(removed_secrets)}")
            elif provided_secrets:
                print(f"  - {name}: will migrate, secret field(s) filled from --secrets-file "
                      f"(no manual retyping needed): {', '.join(provided_secrets)}")
            elif removed_secrets:
                print(f"  - {name}: will migrate, but secret field(s) stripped -> retype in dest: {', '.join(removed_secrets)} "
                      f"(or supply the real value via --secrets-file to automate this)")
            elif is_role and "policies" in obj:
                print(f"  - {name}: will migrate as pass 1 (bare); policies reattached in phase F. "
                      f"aaa-bw-contract (if present) is permanently dropped -- no working API found for it")
            else:
                print(f"  - {name}: will migrate")

            for ref_path, ref_label, ref_name in scan_references(obj):
                all_findings.append((otype["label"], name, ref_path, ref_label, ref_name))

            keep_list.append((cleaned, obj))
        all_cleaned[slug] = keep_list

    # ---- Reference-check report ----
    print("\n" + "-" * 78)
    print("Reference check -- every named pointer to another object this script")
    print("found, so you can confirm the target exists in DEST (or is a system")
    print("object that's already there, or is one of the object types this")
    print("script couldn't reach and you'll need to hand-configure):")
    print("-" * 78)
    if not all_findings:
        print("  none found")
    else:
        for otype_label, obj_name, ref_path, ref_label, ref_name in all_findings:
            print(f"  {otype_label} '{obj_name}' -> {ref_path} = '{ref_name}' ({ref_label})")

    if not args.apply:
        print("\nDry run complete. Nothing was written to any destination tenant.")
        print("Re-run with --apply once DST_BASE_URL / DST_TOKEN are set and you've")
        print("reviewed the skip list, secrets checklist, and reference check above.")
        return

    # ---- Apply, phase by phase ----
    print("\n" + "=" * 78)
    print("Applying to destination tenant...")
    print("=" * 78)

    by_list_key = {t["list_key"]: t for t in types}

    def apply_phase(phase_letter):
        for otype in types:
            if otype["phase"] != phase_letter:
                continue
            items = all_cleaned.get(export_slug(otype), [])
            if not items:
                continue
            print(f"\n[Phase {phase_letter}] {otype['label']}:")
            for cleaned, raw in items:
                name = get_name(cleaned, otype)
                if otype["list_key"] == "role" and name in system_protected_role_names:
                    print(f"  SKIP create (system-protected role, already exists in every "
                          f"tenant): {name} -> policies will be reattached in phase F")
                    continue
                # Confirmed live 2026-09-11 by testing directly against a real
                # tenant (create-then-verify-then-delete, on dns and roles):
                # POST to the bare collection wants EXACTLY the same envelope
                # shape GET returns -- {list_key: [...]} -- just with a single-
                # element array containing the one new object. Earlier guesses
                # (bare object; wrapped-but-not-a-list; wrapped under the URL
                # slug instead of list_key) all failed with increasingly
                # specific schema errors; the final one ("Expecting JSON
                # name/array of objects but list '<list_key>' is represented
                # in input data as name/object") is what nailed this shape.
                envelope = {otype["list_key"]: [cleaned]}
                status, resp = api_call(dst_base, dst_token, "POST", otype["list_path"], body=envelope)
                if status in (200, 201):
                    print(f"  OK  {name} -> HTTP {status}")
                elif is_already_default_error(resp):
                    print(f"  SKIP (already a protected library default in DEST): {name} -> {resp.get('message')}")
                elif is_already_exists_error(resp):
                    if args.no_refresh_existing:
                        print(f"  SKIP (already exists in DEST, likely from a prior run): {name} -> {resp.get('message')}")
                    else:
                        # Confirmed live 2026-09-13: "already exists" here means
                        # an earlier run of THIS SCRIPT already created the
                        # object (never a genuine Aruba-shipped default -- those
                        # 400 with "cannot change default config" instead, see
                        # is_already_default_error, and are never touched here).
                        # Across a long, iterative migration with many partial
                        # --only runs, an object can easily have been created
                        # before some dependency its OWN content refers to
                        # (e.g. a Role named in a Policy rule's condition) was
                        # itself in place yet -- and a plain create-or-skip
                        # would leave that stale/incomplete copy in DEST
                        # forever, since every later run just sees the name
                        # collision and skips. So: try to push the CURRENT
                        # source content in via PUT, the same {list_path}/{name}
                        # + bare-object shape already confirmed for role phase F.
                        put_url = f"{otype['list_path']}/{urllib.parse.quote(str(name), safe='')}"
                        put_status, put_resp = api_call(dst_base, dst_token, "PUT", put_url, body=cleaned)
                        if put_status in (200, 201, 204):
                            print(f"  OK  (refreshed existing object's content in DEST): {name} -> HTTP {put_status}")
                        else:
                            print(f"  SKIP (already exists in DEST; attempted to refresh its content "
                                  f"but that also failed -- create: {resp.get('message')} | "
                                  f"update attempt: HTTP {put_status} {put_resp}): {name}")
                elif is_vault_secret_error(resp):
                    print(f"  SKIP (secret encrypted with SOURCE tenant's vault key -- can't be "
                          f"decrypted in DEST; recreate this exact object by hand in DEST with its "
                          f"real value, then re-run --apply): {name} -> {resp.get('message')}")
                elif is_missing_dependency_error(resp):
                    print(f"  ERR (missing dependency in DEST -- a referenced library "
                          f"object doesn't exist there yet, e.g. a Central NAC "
                          f"auto-provisioned object or a Network Group; see docstring) "
                          f"{name} -> {resp.get('message')}")
                else:
                    print(f"  ERR {name} -> HTTP {status} {resp}")
                time.sleep(0.2)

    for phase in ["A", "B", "C", "D", "E"]:
        if any(t["phase"] == phase for t in types):
            apply_phase(phase)

    # ---- Phase F: reattach role -> policy links ----
    if "role" in by_list_key and not args.skip_role_repatch:
        role_type = by_list_key["role"]
        role_items = all_cleaned.get(export_slug(role_type), [])
        reattach = [(cleaned, raw) for cleaned, raw in role_items if "policies" in raw]
        if reattach:
            print(f"\n[Phase F] Reattaching policies to {len(reattach)} role(s) "
                  f"(PUT {role_type['list_path']}/{{name}}):")
            reattach_ok = 0
            reattach_default_skip = 0
            for cleaned, raw in reattach:
                name = raw.get("name")
                patched = json.loads(json.dumps(cleaned))
                patched["policies"] = raw["policies"]
                url = f"{role_type['list_path']}/{urllib.parse.quote(str(name), safe='')}"
                # Confirmed live 2026-09-11 directly against a real tenant:
                # PUT to a name-suffixed URL (a specific existing list entry,
                # as opposed to POST to the bare collection) takes the BARE
                # object with no envelope at all -- the URL already identifies
                # which entry this is. This is the opposite shape from the
                # POST-to-collection case above; don't "fix" it to match.
                #
                # NOTE: this PUT was only confirmed against ordinary, non-
                # system roles (e.g. "BFK"). As of 2026-09-11 it has NOT yet
                # been confirmed whether the destination also accepts a PUT
                # to a system_protected_role_names entry (e.g. "logon") --
                # i.e. whether the API allows reattaching policies on a
                # protected default role even though it refuses to let you
                # create/replace one outright. If this 400s with something
                # like "cannot change default config" or "cannot change
                # protected role", that means this specific reattachment
                # isn't possible via the API and has to be done by hand in
                # the destination tenant's UI -- report the exact message
                # and this can be documented as a hard limitation rather
                # than treated as a bug.
                status, resp = api_call(dst_base, dst_token, "PUT", url, body=patched)
                if status in (200, 201, 204):
                    print(f"  OK  {name} -> HTTP {status}")
                    reattach_ok += 1
                elif is_already_default_error(resp):
                    print(f"  SKIP (already a protected library default in DEST): {name} -> {resp.get('message')}")
                    reattach_default_skip += 1
                elif is_already_exists_error(resp):
                    print(f"  SKIP (already exists in DEST, likely from a prior run): {name} -> {resp.get('message')}")
                elif is_vault_secret_error(resp):
                    print(f"  SKIP (secret encrypted with SOURCE tenant's vault key -- can't be "
                          f"decrypted in DEST; recreate this exact object by hand in DEST with its "
                          f"real value, then re-run --apply): {name} -> {resp.get('message')}")
                elif is_missing_dependency_error(resp):
                    print(f"  ERR (missing dependency in DEST -- a referenced library "
                          f"object doesn't exist there yet, e.g. a Central NAC "
                          f"auto-provisioned object or a Network Group; see docstring) "
                          f"{name} -> {resp.get('message')}")
                else:
                    print(f"  ERR {name} -> HTTP {status} {resp}")
                time.sleep(0.2)
            # Confirmed live 2026-09-13 on a second tenant: PUT (not just POST)
            # can be rejected with "cannot change default config" for EVERY
            # single protected role attempted -- i.e. this destination
            # tenant's API refuses to modify these roles' policy lists at
            # all, not just refuses to recreate them. If that happened this
            # run, surface it loudly instead of leaving it buried in a wall
            # of per-role lines: it means none of these roles' custom policy
            # attachments got applied, and the only way to attach them on
            # THIS tenant is by hand in the destination UI. This is a
            # per-tenant/API platform limitation, not a bug in this script --
            # but it needs to be visible, not just present in the log.
            if reattach and reattach_ok == 0 and reattach_default_skip == len(reattach):
                print(f"\n  *** WARNING: ALL {len(reattach)} role(s) above were rejected as "
                      f"\"already a protected library default\" -- ZERO succeeded. This "
                      f"destination tenant's API appears to refuse ANY modification "
                      f"(create OR update) to these built-in-named roles, so none of "
                      f"their custom policy attachments (see the reference-check report "
                      f"above for exactly which Policy belongs on which Role) made it "
                      f"across. This has to be done by hand in the destination tenant's "
                      f"New Central UI -- open each role listed above and attach the "
                      f"policies the reference-check report shows for it. To sanity-check "
                      f"this isn't a token/permission-scope issue instead of a hard "
                      f"platform limitation, try editing one of these roles by hand in "
                      f"the destination UI yourself -- if the UI lets a human do it, the "
                      f"restriction is specific to API tokens on this tenant, which your "
                      f"GreenLake admin may be able to grant a broader scope for.")
        # This check is independent of whether any role also had "policies" --
        # a role can carry a bw-contract with no policies at all, and it still
        # needs to be flagged since it's dropped permanently either way.
        skipped_bw = [raw.get("name") for _, raw in role_items if "aaa-bw-contract" in raw]
        if skipped_bw:
            print(f"\n  NOTE: aaa-bw-contract was NOT reattached for: {', '.join(skipped_bw)} "
                  f"-- no working Bandwidth Contract API path found; recreate that binding by hand.")
    elif "role" in by_list_key and args.skip_role_repatch:
        print("\n[Phase F] Skipped (--skip-role-repatch). Reattach role->policy links by hand in the UI.")

    if "wlan-ssid" in by_list_key:
        apply_phase("G")

    print("\nDone. Any secret field(s) still flagged \"needs manual retype\" above")
    print("(auth server shared secrets, WPA personal-security passphrases, and")
    print("secret-holding Aliases like a RADIUS key) did NOT come across and need")
    print("to be retyped by hand in the destination tenant's UI -- OR supply their")
    print("real values via --secrets-file and re-run to have this script write")
    print("them in directly instead. Also check the")
    print("reference-check report for anything pointing at Bandwidth Contracts,")
    print("802.1X/MAC-auth profiles, or AP Certificate Usage -- this script")
    print("could not find a working API path for those three object types.")
    print("Any WLAN ERR mentioning \"missing dependency in DEST\" or the")
    print("validation message about primary-auth-server/auth server group")
    print("is a Central NAC (cloud-auth) WLAN -- see the docstring's CENTRAL")
    print("NAC section. Those need Central NAC onboarded in the destination")
    print("tenant (via its own UI) before this script can create them.")


if __name__ == "__main__":
    main()
