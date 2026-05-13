# Release cycle runbook

The operator-side dance for landing a change from working tree to
prod. Every release goes through the same eight phases:

1. **Pre-flight** — make sure local `main` matches origin
2. **Branch + commit(s)** — one branch, one or more focused commits
3. **Push the branch**
4. **Deploy to staging**
5. **Verify on staging**
6. **Tag** the verified commit
7. **Promote** the tag to prod
8. **Merge + clean up** — keep `main` = "what's in prod"

This document walks through a hypothetical `v1.7.0` release with two
distinct commits, calling out the rationale + the common footguns
at each step. Companion to
[`docs/staging_environment.md`](staging_environment.md), which
describes the architecture; this is the operator runbook on top.

---

## Hypothetical scenario

Two unrelated fixes landing together:

* **Backend fix** — `routes_app_form.py` corrects a small validation
  bug. Pure Python; backend tests cover it.
* **Frontend tweak** — `static/app/app.js` adds a UI affordance.
  No tests in CI; verified visually.

The two are independent enough to keep as separate commits (so
either is revertable on its own) but small enough to ship together
in one release.

---

## Phase 1 — Pre-flight

```bash
git fetch origin
git status                       # working tree should be clean
git checkout main
git pull origin main             # fast-forward to origin/main
```

**Why every time?** The "stale local main" trap was real history
in this repo — `tools/stage deploy main` once deployed an old
commit because local `main` was 24 commits behind `origin/main`.
Fetch + pull every cycle, no exceptions.

If `git status` is *not* clean, deal with that first — either commit
those changes (on a different branch), stash them
(`git stash push -m "wip notes"`), or discard them
(`git checkout -- .`, but only if you're sure). Never branch off a
dirty tree.

---

## Phase 2 — Branch + commits

```bash
git checkout -b v1.7.0-bundle
```

**Naming.** The branch name doesn't have to match the tag — branch
is `v1.7.0-bundle` (or `v1.7.0-feature`, or whatever describes the
work), tag is just `v1.7.0`. They share a refname namespace at
push time so they can't be byte-identical, but a suffix avoids
the conflict cleanly.

### Commit 1 — backend fix

```bash
# Apply the change (edit files, copy from sandbox, etc.)
# Then:
git status                       # confirm only the expected files moved
git diff                         # eyeball — should match what you expect
git add backend/src/api/routes_app_form.py \
        backend/tests/test_app_form.py
git commit -m "Fix validation bug in form-submit decoder"
git log --oneline -3             # confirm the commit landed
```

**Why named files for `git add` instead of `git add -A` or `git add .`?**
Stops a stray scratch file (`.env.local`, debug log, editor swap
file) from sneaking into the commit.

**Why `git diff` before `git add`?** The 5-second eyeball check
catches "wait, why did THIS file change?" surprises before they
become commits. Especially valuable when copying from a sandbox
or merging from another branch.

### Commit 2 — frontend tweak

```bash
git status                       # should be clean from the prior commit
# Apply the change.
# **Frontend rule of thumb**: if you touched anything under
# `backend/src/static/app/`, you also need to bump the cache-bust
# version in `landing.html` and `device.html`. Search for `app.js?v=`
# to find the references; bump both. Static assets get aggressively
# cached at the browser AND at Cloudflare's edge, and a stale
# `?v=N` will silently serve the old code even after a fresh
# deploy. (See "Pitfall 1" below.)
git status                       # 3 files: app.js + landing.html + device.html
git diff                         # confirm the cache-bust bump is there
git add backend/src/static/app/app.js \
        backend/src/static/app/landing.html \
        backend/src/static/app/device.html
git commit -m "Add UI affordance for X (cache-bust v=N → v=N+1)"
git log --oneline -3             # two new commits on top of main
```

**Two commits, not one.** They're independent fixes — commit
discipline buys you cheap reverts. If after deploy you find
commit 2 broke something, `git revert <sha>` of just that commit
is straightforward; if both fixes shared a commit, you'd have
to peel them apart by hand.

---

## Phase 3 — Push the branch

```bash
git push -u origin v1.7.0-bundle
```

`-u` sets the upstream so future `git push` / `git pull` on this
branch don't need the remote/branch arguments.

**Verify:** the output shows `* [new branch] v1.7.0-bundle -> v1.7.0-bundle`.

---

## Phase 4 — Deploy to staging

```bash
tools/stage deploy origin/v1.7.0-bundle
```

**The `origin/` prefix matters.** `tools/stage deploy v1.7.0-bundle`
would expect a *local* branch by that name on the staging host,
which won't exist — the branch only exists on origin. The script
runs `git fetch && git checkout -B staging-current <ref>` on the
staging host, so passing the fully-qualified remote ref is the
right shape.

**What this does:**
- Fetches latest tags + branches on the staging host
- `git checkout -B staging-current origin/v1.7.0-bundle` (force-create
  the staging branch at the new tip)
- `docker compose build && up -d` (rebuild the image, recreate
  containers from the new image)
- Runs `tools/smoke_test.sh` against staging hostnames

**Verify:** wait for the script to finish; expect 9/9 or 10/10
green smokes. If the smoke fails, fix the root cause before
moving forward — do NOT tag a failing build.

---

## Phase 5 — Verify on staging

This is the load-bearing step — never skip it before tagging.
What "verify" means depends on the change:

* **Backend changes** → existing backend test suite covers most of
  it; the staging verify is "does the smoke test pass + does the
  endpoint behave when I curl it / click through it."
* **Frontend changes** → no JS tests in CI for app.js, so visual
  verification on staging is the only pre-prod confidence.
* **Bug fixes** → reproduce the bug's specific repro on staging
  with the fix in place; confirm the bug is gone.
* **CLI / tools changes** → exercise inside `tools/stage bash` (the
  CLI is baked into the image at build time).

**Hard-reload the browser** (Cmd+Shift+R / Ctrl+Shift+R) when
you're verifying frontend changes. Browsers can serve stale HTML
from back-button cache even with `Cache-Control: no-store` on
the response.

If verification surfaces a new bug — file it, decide whether to
ship the existing branch (file the bug as a TODO for a later
release) or roll the fix into the in-flight branch (commit it,
push it, redeploy). The cycle is cheap enough that another spin
through phases 4-5 is fine.

---

## Phase 6 — Tag

```bash
git tag v1.7.0 origin/v1.7.0-bundle
git push origin v1.7.0
git tag --list 'v1.7.*'          # confirm the tag is in the list
```

**Why tag *after* staging verifies?** `tools/stage promote` does a
"trust but warn" check: the tag's commit must match `staging-current`'s
HEAD on the staging host, otherwise it warns *"You may be promoting
code that wasn't verified on staging."* Tagging the exact commit
you just verified keeps that invariant clean.

**Why use `origin/v1.7.0-bundle` as the tag target instead of
`HEAD`?** They should be the same SHA at this point, but explicit
> implicit. Same reason `git diff origin/main..HEAD` is safer than
relying on whatever your terminal's `HEAD` is.

---

## Phase 7 — Promote to prod

```bash
tools/stage promote v1.7.0
```

**What this does:**
- On the prod host: `git fetch --tags`
- Errors out if `refs/tags/v1.7.0` doesn't exist on origin
- `git checkout -B deploy v1.7.0`
- `docker compose build && up -d`
- Reports the new HEAD

**Verify:** the script prints something like `"Prod promoted to
v1.7.0"` plus the deploy branch HEAD. Then run a prod smoke check
(`tools/smoke_test.sh` against prod hostnames, or manual
click-through of the affected surfaces).

---

## Phase 8 — Verify on prod

Same shape as staging verify, but against the prod hostnames.
The most common gotchas in this phase:

* **CDN cache lag.** If the change touched static assets and the
  cache-bust version was bumped (you did bump it, right?), prod
  customers' browsers will fetch the new `?v=N+1` URL fresh — no
  purge needed. If you forgot to bump, customers will keep hitting
  the cached old version until manual purge or natural eviction.
* **Real-data shapes.** Staging has whatever subset of fixtures
  you've fed it; prod has actual customer data with edge cases
  staging didn't surface. A change that worked on staging can
  still surprise on prod's data shapes.
* **Auth.** Prod's OAuth + Cloudflare zone setup may have settings
  staging doesn't (Bot Fight Mode, Web Analytics injection,
  Email Address Obfuscation). Check the browser console after
  the first prod load; CSP violations from CF-injected scripts
  are a common surprise.

If something's wrong on prod, decide: roll forward (commit a fix
to a new branch, ship `v1.7.1`) or roll back. Roll-back path:

```bash
# On the prod host (or via tools/stage promote with the previous tag):
tools/stage promote v1.6.5             # or whatever the prior tag was
```

The image rebuilds at the prior tag's code, prod is back to
known-good. Then dig into what went wrong on a fresh branch off
main.

---

## Phase 9 — Merge + clean up

Once prod is stable, get `main` back to "what's currently shipped."

```bash
git checkout main
git pull origin main             # cheap; in case anything else landed
git merge v1.7.0-bundle          # fast-forward expected (no main movement
                                 # during the cycle = clean FF merge)
git push origin main
git branch -d v1.7.0-bundle              # local; -d refuses if not merged
git push origin --delete v1.7.0-bundle   # remote
```

**Why `-d` not `-D` for the local delete?** Lowercase `-d` refuses
to delete an unmerged branch, so it's a built-in "did I actually
merge this?" check. Uppercase `-D` force-deletes; only reach for
it if you really mean to throw work away.

**Why merge instead of rebase?** Short-lived branches with no
parallel main movement merge cleanly as fast-forwards. Rebase is
for the case where main has drifted under your branch and you want
linear history before merging — not the typical case for the
cadence this repo runs at. (If you ever do need it, see
[git-scm rebase docs](https://git-scm.com/docs/git-rebase) — and
remember the cardinal rule: never rebase a branch other people
might have pulled.)

---

## Common pitfalls

### Pitfall 1: stale `app.js?v=N` cache-bust

**Symptom.** Frontend change deployed, but browsers still execute
the old code. Hard-reload doesn't help. Symptom is often "the new
JS function I added doesn't exist" or "old behavior persists
after deploy."

**Cause.** `device.html` and `landing.html` reference the JS as
`<script src="/app/_static/app.js?v=N">`. The `?v=N` is the
cache-bust query parameter. If you change `app.js` but don't bump
`N`, the browser/CDN serve the cached `?v=N` content keyed on the
URL, regardless of the file's actual content.

**Prevention.** Whenever you touch `static/app/app.js`, bump
`?v=N` to `?v=N+1` in BOTH `landing.html` AND `device.html`. Search
for `app.js?v=` to find the references; the version should be
identical across both files.

**Automated check (post-v1.6.8):** the repo ships a pre-commit hook
at `.githooks/pre-commit` that catches missed cache-bust bumps at
`git commit` time. Install once per clone:

```bash
git config core.hooksPath .githooks
```

When the hook detects a JS or CSS edit without a matching
`?v=N` bump in the referrer HTML(s), it errors with a specific
message naming the file pair. Bypass with `git commit --no-verify`
if you're sure the change doesn't affect runtime (rare).

**Diagnostic when stuck.** Check what's served vs. what you expect:

```bash
# From your dev host, hit the staging or prod static URL directly:
curl -s 'https://stra2us-staging.austindavid.com/app/_static/app.js?v=N' \
    | grep -c '<distinctive new code marker>'
# Returns 0 → server is serving stale content (deploy issue or wrong path)
# Returns 1+ → server has new code; browser/CDN is the stale layer
```

### Pitfall 2: `tools/stage deploy main` deployed wrong commit

**Symptom.** Staging behavior doesn't reflect a recent commit you
made to `main`.

**Cause.** Local `main` was behind `origin/main`. `tools/stage deploy
main` resolves to the local ref (which is your stale checkout) on
the staging host's clone if it was fetched at a different time.

**Prevention.** `git fetch && git pull origin main` before
branching, OR pass the fully-qualified remote ref:
`tools/stage deploy origin/main`.

### Pitfall 3: tag points to a different commit than what was verified

**Symptom.** `tools/stage promote v1.X.Y` warns *"tag commit ≠
staging-current."*

**Cause.** Tagging happened at a different commit than what was
deployed to staging. Usually because docs-only edits landed on
the branch tip after staging deploy but before tagging.

**Prevention.** Tag from `origin/<branch>` after staging verifies,
not from local HEAD. If docs-only changes happened on the branch
post-staging-verify, decide: are you OK promoting them un-staged
(usually yes, docs don't affect runtime), or do you want to
re-deploy staging at the latest tip first (cleaner)?

### Pitfall 4: `provision_device` doesn't make device discoverable

**Symptom.** New device provisioned via admin UI / CLI shows up
correctly in admin views, but the customer landing form's name
lookup returns *"No device named X."*

**Cause.** `provision_device` only writes the ACL + secret;
`lookup_device` establishes "device exists" by scanning for any
KV record under `<app>/<name>/`. A device that's been provisioned
but hasn't done its first KV write is invisible to the lookup.

**Workaround.** Write any KV value for the device — admin UI's
Set KV, or `stra2us set <device> <key> <value>` from inside the
staging container. One write makes the device discoverable.

(Filed as a TODO for a proper reverse-index fix.)

---

## Quick reference

```
# Pre-flight
git fetch origin && git checkout main && git pull
git checkout -b <branch>

# Commit (repeat per logical change)
git status; git diff
git add <files>
git commit -m "<message>"

# Push + deploy
git push -u origin <branch>
tools/stage deploy origin/<branch>

# Verify on staging — don't skip!

# Tag + promote
git tag <tag> origin/<branch>
git push origin <tag>
tools/stage promote <tag>

# Verify on prod — don't skip!

# Cleanup
git checkout main && git pull
git merge <branch>
git push origin main
git branch -d <branch>
git push origin --delete <branch>
```

The whole cycle is ~10 commands once it's muscle memory. Most of
the time spent in a release should be the verify steps (5 and 8),
not the git plumbing.

## After .gitignore...

`git ls-files -i -c --exclude-standard`
