# CLAUDE.md

## Releases — the owner decides (owner, 2026-10-10)

This repo is public on GitHub. **Never create a new version on your own.** That means: no
`/deploy`, no VERSION bump, no CHANGELOG release heading, no git tag, no GitHub release — unless
the owner explicitly asks for a release in the current conversation ("release", "deploy",
"new version"). Finishing a feature or a fix is not a request to release: commit it, then ask.

When the owner asks for a release:

1. Run `/deploy` (it calls `infrastructure/scripts/deploy.sh`). Show the `--dry-run` output first.
2. The script needs `--owner-release` here (`homelab.yml`: `release_by: owner`). Pass it only for
   a release the owner asked for in this conversation.
3. The script bumps VERSION, writes the CHANGELOG section, commits `chore(release): X.Y.Z`,
   **tags that commit `X.Y.Z`** (no `v`), pushes, and **creates the GitHub release** with the
   CHANGELOG section as notes (`github_release: true`). Check the release exists:
   `gh release view X.Y.Z`.

Keep notable changes under `## [Unreleased]` in CHANGELOG.md between releases — the next
release uses that text.
