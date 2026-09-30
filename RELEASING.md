# Releasing tryaii

## Overview

`tryaii` ships as two packages from one repo: `tryaii` on npm (`packages/node`) and
`tryaii` on PyPI (`packages/python`). Both share a single version, stored in four
files: `packages/node/package.json`, `packages/node/package-lock.json`,
`packages/python/pyproject.toml`, `packages/python/tryaii/__init__.py`. Pushing a
`vX.Y.Z` tag runs `.github/workflows/release.yml`, which checks the tag against
all four spots, builds and smoke-tests both packages, waits for a human to approve
the `release` environment, publishes to PyPI and npm via Trusted Publishing (OIDC,
no tokens anywhere), and creates a GitHub Release from the matching CHANGELOG section.

A normal release:

1. On a branch, bump the version and add a `## X.Y.Z (YYYY-MM-DD)` section to `CHANGELOG.md`:

   ```bash
   python scripts/bump-version.py X.Y.Z
   python scripts/bump-version.py --check X.Y.Z
   ```

2. Open a PR to `main`, get one approval, merge.
3. Tag the merge commit (annotated) and push the tag. Only repo admins can do this:

   ```bash
   git checkout main && git pull
   git tag -a vX.Y.Z -m "release: X.Y.Z"
   git push origin vX.Y.Z
   ```

4. Open the run under Actions, click **Review deployments**, tick `release`, **Approve and deploy**. When the run is green, the release is done.

## One-time setup

Trusted Publishing means the registry trusts a GitHub identity (repo + workflow + environment) instead of a long-lived token. Nothing is copied into GitHub secrets. All three steps below must be done once, by someone with admin rights on the repo and owner rights on the registries, before the first tagged release. Until they are done the workflow fails at the publish step and nothing is uploaded.

### PyPI (existing project `tryaii`)

1. Log in at pypi.org, open **Your projects → tryaii → Manage → Publishing**.
2. Under **Add a new publisher → GitHub**, fill in:
   - Owner: `Tryaii-Admin`
   - Repository name: `tryaii`
   - Workflow name: `release.yml`
   - Environment name: `release`
3. Click **Add**. No API token is created; PyPI will accept uploads whose OIDC identity matches exactly those four values.

### npm (package `tryaii`)

1. Log in at npmjs.com, open the `tryaii` package page → **Settings** → **Trusted Publisher**.
2. Choose **GitHub Actions** and fill in:
   - Organization or user: `Tryaii-Admin`
   - Repository: `tryaii`
   - Workflow filename: `release.yml`
   - Environment name: `release`
3. Save. Then, on the same Settings page under **Publishing access**, select **Require two-factor authentication and disallow tokens** (trusted publisher only). Recommended: it makes the workflow the only way to publish, so a leaked personal token cannot push a release.

### GitHub (needs the repo admin, i.e. the `Tryaii-Admin` account)

1. **Settings → Environments → New environment** named `release`. Tick **Required reviewers** and add the maintainer who approves releases. Under **Deployment branches and tags** choose *Selected*, add a **tag** rule `v*`.
2. **Settings → Rules → Rulesets → New tag ruleset** named `release-tags`: target tags matching `v*`; enable *Restrict creations*, *Restrict updates*, *Restrict deletions*; bypass list: repository admins.
3. **Settings → Rules → Rulesets → New branch ruleset** named `main-protection`: target the default branch; enable *Require a pull request before merging* with 1 approval and *Dismiss stale approvals*, plus *Restrict deletions* and *Block force pushes*; bypass list: repository admins.

Equivalent `gh api` calls are in the PR that introduced this file. Collaborators with only *Write* access cannot do this; as of 2026-09-30 that is every collaborator except the owner account.

## Security model

- The workflow file is public and that is fine: there are no secrets to leak. Publishing uses a short-lived OIDC token GitHub mints for that specific run, which PyPI and npm exchange for a one-shot upload credential.
- Forks and pull requests cannot publish. The OIDC identity encodes the repository (`Tryaii-Admin/tryaii`), the workflow file (`release.yml`) and the environment (`release`). A fork has a different repository name, and PR-triggered runs never get the `release` environment, so both registries reject them.
- The manual approval gate is the last human check. The `release` environment pauses the run after build and smoke tests; a listed reviewer must approve before the publish jobs get the OIDC token. Someone who can push a tag still cannot publish alone.
- Third-party actions are pinned to full commit SHAs, not tags. A tag like `v4` can be moved to malicious code; a SHA cannot.
- The remaining risk is anyone with write access: they could push a tag or edit the workflow. The `v*` tag ruleset (admins only) and the PR-with-approval rule on `main` narrow that to admins plus a reviewer.

## Troubleshooting

**Version check fails ("tag v0.6.0 does not match ...")**
The four version spots disagree with each other or with the tag. Run `python scripts/bump-version.py --check` locally; it prints each spot. Fix on a branch, merge, then tag again with a new version (see below). Do not move an existing tag.

**PyPI: "invalid-publisher" / "Trusted publisher not configured"**
The publisher fields on pypi.org do not match the run exactly. Compare owner, repo, workflow file name and environment name character for character (they are case-sensitive; the environment must be `release`).

**npm: E404 or "trusted publisher" / provenance errors**
Check the npm Trusted Publisher settings match the same four values. Also confirm the workflow job has `id-token: write` permission and runs inside the `release` environment.

**A publish job failed partway (for example PyPI succeeded, npm failed)**
Versions are immutable on both registries: a version once published can never be reused, even if deleted. Never try to delete or overwrite. Fix the cause, bump to the next patch version, and run a normal release. It is acceptable for one registry to skip a version number.

**Emergency manual publish (GitHub Actions unavailable)**
Only when Actions is down. If npm is set to trusted-publisher-only, temporarily relax it in package Settings first, then restore it.

```bash
# npm
npm login
npm publish ./packages/node

# PyPI
cd packages/python
python -m build
twine upload -u __token__ -p <pypi-token> dist/*
```

Legacy gotchas from token-based releases:

- npm `E404` on publish almost always means you are logged out; run `npm login` again.
- PyPI `403` usually means the username is not exactly lowercase `__token__`, or the token is scoped to a different project.
- Hatchling honours `.gitignore`. An unanchored pattern such as `data/` excludes every `data` directory from the wheel, including package data. Anchor patterns (`/data/`) and inspect the built wheel with `unzip -l dist/*.whl` before uploading.

## Rollback

Nothing is ever deleted from a registry. Deleting breaks anyone who already pinned the version and frees nothing, since the version number stays burned.

- PyPI: on pypi.org open **Manage → Releases → X.Y.Z → Options → Yank**. Existing pins keep working; `pip install tryaii` skips the yanked release.
- npm:

  ```bash
  npm deprecate tryaii@X.Y.Z "Broken: use X.Y.Z+1"
  ```

  Installs still succeed but print the warning.

Then ship the fix as a new patch version through the normal release flow.
