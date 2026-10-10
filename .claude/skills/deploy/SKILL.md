---
name: deploy
description: Bump version, tag, build, publish, verify health, auto-roll-back on failure. Use when the user asks to "deploy", "ship it", "publish", or "go live". Thin wrapper around the shared logic in the infrastructure repo — see WEBAPP_PROJECT_STANDARD.md §9.
---

# Deploy

This is a thin wrapper. All the actual logic — version bump, changelog,
tagging, build, publish, healthcheck, auto-rollback on failure — lives once
in `infrastructure/scripts/deploy.sh`, not here. Don't duplicate it into this
file; if something about deploy behavior needs to change, change it there so
every project gets the fix.

## 0. Only when the owner asked for a release

This repo releases **only on the owner's command** (CLAUDE.md, `homelab.yml` `release_by: owner`).
If the owner did not ask for a release in this conversation, stop here and ask. When they did,
the real run needs `--owner-release`; it also creates the GitHub release.

## 1. Confirm before running

**Ask before deploying — this reaches whoever uses the app.** Show what
would happen first:

```bash
bash "${INFRA_DIR:-/samba/projects/infrastructure}"/scripts/deploy.sh --dry-run
```

> **Path note:** `$INFRA_DIR` defaults to the infrastructure repo's checkout and is set for you by
> `deploy-common.sh`. Prefer it over a hardcoded absolute path — this file is committed to the app's
> own repo, and some of those repos are public (WEBAPP_PROJECT_STANDARD.md §1), so a literal server
> path would be published along with it. Set `INFRA_DIR` explicitly if the checkout ever moves.


This prints the version bump it would make and what it would publish,
without writing, tagging, or publishing anything. Show the user this output
and get an explicit go-ahead before the real run.

## 2. Deploy for real

```bash
bash "${INFRA_DIR:-/samba/projects/infrastructure}"/scripts/deploy.sh [local|public|both]
```

Omit the target for Profile A (container) apps, or for Profile B apps whose
`deploy.config` only declares one target. `--bump patch|minor|major`
overrides the automatic version-bump detection if the commit-message
heuristic guesses wrong.

## 3. Report the result

- **Success**: report the new version and, for Profile B, which target(s)
  published.
- **Failure**: the script's own error message says exactly what happened and
  whether anything was rolled back — relay that verbatim rather than
  summarizing, the details (which version things landed on, whether the tag
  made it to origin) matter for figuring out what to do next.
