---
name: falafacil-release
description: Release and publish a new version of FalaFácil to GitHub Releases and Homebrew Tap. Use when the user mentions "nova versão", "lançar release", "publicar versão", "fazer release", "bump de versão", "criar tag de release", /falafacil-release, "release", "bump version", "create tag", "publish release", or "deploy new version".
---

# FalaFácil Release Workflow

Automates the complete release lifecycle for FalaFácil: pre-flight checks, pendencies verification, dynamic contract discovery, validation gates, Git tagging, GitHub Release publishing, Homebrew formula synchronization, and clean installation verification.

See [docs/RELEASE.md](../../../docs/RELEASE.md) for authoritative reference, edge cases, and troubleshooting.

## Workflow

### 1. Pre-flight Checks, Version Selection & Release Summary
1. Perform non-destructive pre-flight check on `main`:
   - Check clean status and branch: `git status --porcelain` (must be clean) and `git branch --show-current` (must be `main`). Stop if dirty or on another branch.
   - Fetch remote and verify divergence: `git fetch origin main && git rev-list --left-right --count main...origin/main`.
   - Fast-forward only after clean evidence: `git pull --ff-only origin main`.
2. Verify GitHub CLI auth (`gh auth status`), require `PUBLIC` visibility for both repos (`gh repo view OthonBreener/falafacil --json visibility -q .visibility` and `gh repo view OthonBreener/homebrew-falafacil --json visibility -q .visibility` return `PUBLIC`), and require immutable releases policy enabled (`gh api repos/OthonBreener/falafacil/immutable-releases --jq .enabled` returns `true`).
3. Check `HOMEBREW_TAP_TOKEN` secret exists via `gh secret list` and confirm explicit human confirmation that stored PAT scope is tap-only `Contents: Read and write` (CLI cannot inspect token scope). *Never expose token value.*
4. Consult and resolve pending release items (`docs/PENDENCIAS.md`):
   - Read `docs/PENDENCIAS.md` to verify if there are active pendencies or planned tasks for upcoming releases.
   - If active pendencies exist:
     - Implement the pending changes and corresponding tests (role `implementador`).
     - Run verification suite and smoke validation (role `testador`, requires `PASS`).
     - After `PASS`, remove the resolved item from `docs/PENDENCIAS.md` (role `implementador`). When all pendencies are resolved, ensure `docs/PENDENCIAS.md` is clean (containing only `# Pendências para próximas releases\n\nNenhuma pendência no momento.`).
     - Review complete diff including the cleaned `docs/PENDENCIAS.md` and test evidence (role `revisor`, requires `APROVADO`).
     - Commit the implemented pendencies to `main` branch (role: principal / release operator) before initiating version bump.
   - If `docs/PENDENCIAS.md` is clean (`Nenhuma pendência no momento.`), proceed with release summary and version selection.
5. On synchronized `main`, analyze latest tag (`git describe --tags --abbrev=0`), commits and diff since latest tag (`git log <latest_tag>..HEAD --oneline`, `git diff <latest_tag>..HEAD`).
6. Generate concise release summary and select target version `X.Y.Z`: validate target simple SemVer numeric tuple is strictly greater than both current version (`falafacil.__version__`) and latest release tag (`git describe --tags --abbrev=0`); reject equal or lower unused versions. Honor user-specified SemVer if monotonic; otherwise infer PATCH/MINOR/MAJOR conservatively from commits/diff. Ask user only if MAJOR-vs-MINOR ambiguity cannot be resolved.
7. Confirm target version, tag `vX.Y.Z`, and release do not already exist locally or remotely (`git tag -l "vX.Y.Z"`, `git ls-remote --tags origin refs/tags/vX.Y.Z`, `gh release view vX.Y.Z`).

### 2. Version Discovery & Contract Classification (Role: implementador)
1. Discover current version references: `git grep -F "<current_version>"`.
2. Update single version source: `src/falafacil/__init__.py` (`__version__ = "X.Y.Z"`).
3. Classify and update authoritative contracts: `AGENTS.md`, `ARQUITETURA.md`, `tests/test_app.py`, `tests/test_packaging.py`, `tests/test_ui.py` (when version-bound), `docs/agents/smoke-tests.md`.
4. Preserve synthetic fixtures (`tests/test_homebrew_update.py`), error test cases, and historical examples unchanged.

### 3. Local Gate Verification (Role: testador) & Review (Role: revisor)
Run the deterministic verification suite and developer binary smoke:
```bash
poetry install --extras dev --extras build && poetry run pip install --no-deps -e .
QT_QPA_PLATFORM=offscreen poetry run pytest -q
poetry run python -m compileall -q src tests scripts
./scripts/build_executable.sh && ./dist/falafacil --update-probe X.Y.Z
tmp_home=$(mktemp -d) && HOME="$tmp_home" ./scripts/install_desktop.sh "$PWD/dist/falafacil"
env -u GEMINI_API_KEY -u GOOGLE_API_KEY -u LD_LIBRARY_PATH HOME="$tmp_home" QT_QPA_PLATFORM=offscreen timeout 5s "$tmp_home/.local/bin/falafacil" || [ $? -eq 124 ]
```
Confirm `--update-probe` exits 0 and installed binary initializes cleanly (controlled timeout 124 = success). When shortcut service or `PROTOCOL_VERSION` change, also smoke test user systemd/socket and polkit authorization. Role `revisor` audits `git diff`, test evidence, and issues `APROVADO` before release operations.

### 4. Governance Exception, Safe Staging & Commit to main (Role: Principal/Release Operator)
*Governance exception*: Explicit user invocation of this release skill authorizes only the principal/release operator to commit resolved pendencies to `main` (after dedicated implementador code/tests, testador `PASS`, implementador `docs/PENDENCIAS.md` cleaning, and revisor `APROVADO`), and subsequently commit version bump changes, push to `origin main`, and create/push the annotated Git tag `vX.Y.Z` after local gate testador `PASS` and revisor `APROVADO`. Delegated roles (`implementador`, `testador`, `revisor`) remain strictly forbidden from committing, pushing, tagging, or mutating branches/PRs.
1. Enumerate complete repo state with NUL-safe porcelain (`git status --porcelain=v1 -z`). Reject any unexpected untracked `??`, artifacts, credentials, sensitive content, or paths with spaces or leading hyphens unless explicitly reviewed by revisor. Review new-file content safely with an option-delimited mechanism; inspect tracked diff (`git diff --name-only`, `git diff`).
2. Stage safely with option separator, verify staged diff and formatting checks, and commit/push to `main` only:
   ```bash
   git add -A -- && git diff --cached && git diff --cached --check
   git commit -m "chore(release): bump version to X.Y.Z" && git push origin main
   ```

### 5. CI Workflow Trigger, Monitoring & Retry Policy
1. Snapshot existing workflow run IDs first via paginated Actions API (`gh api repos/OthonBreener/falafacil/actions/workflows/release.yml/runs --paginate -q '.workflow_runs[].id'`).
2. Create annotated tag `vX.Y.Z` locally at the approved `main` commit (`git tag -a vX.Y.Z -m "Release vX.Y.Z"`), capture tag commit SHA (`tag_sha=$(git rev-list -n 1 "vX.Y.Z")`), and push it exactly once (`git push origin vX.Y.Z`).
3. Poll with bounded timeout and pagination until exactly one new run matching event `push`, `head_branch == "vX.Y.Z"`, and `head_sha == tag_sha` (not present in snapshot) is found. Fail on zero/multiple runs. Record its ID and URL.
4. Watch exact run to completion: `gh run watch "$run_id" --exit-status`. Confirm explicit success conclusion (`gh run view "$run_id" --json conclusion -q .conclusion` is `success`). Stop on non-success.
5. If tap sync fails transiently after asset publication: snapshot existing run IDs, capture `main_sha=$(git rev-parse HEAD)`, dispatch retry (`gh workflow run release.yml --ref main -f tag=vX.Y.Z`), and poll with bounded timeout and pagination until exactly one new `workflow_dispatch` run matching `head_sha == main_sha` (not in snapshot) is found. Watch via `gh run watch "$retry_run_id" --exit-status` with explicit `success` check. Never use fixed sleep, single immediate query, or reuse tag run ID. *Never manually edit tap repository or re-tag.* Code defects require next PATCH (`X.Y.(Z+1)`).

### 6. Post-Release Validation & Final Output Report
1. Verify GitHub Release JSON: `gh release view vX.Y.Z --json tagName,isDraft,isImmutable,url,assets` (confirm `isDraft: false`, `isImmutable: true`; fail closed if false, null, or unavailable; verify raw binary and tarball assets present).
2. Download public tarball, compute SHA-256 (`sha256sum`), fetch formula SHA from tap (`gh api repos/OthonBreener/homebrew-falafacil/contents/Formula/falafacil.rb -q .content | base64 -d`), and assert exact equality.
3. In clean Ubuntu with Homebrew 6+: `brew update && brew install OthonBreener/falafacil/falafacil && brew test OthonBreener/falafacil/falafacil`.
4. Run exact Homebrew binary `$(brew --prefix)/bin/falafacil` with temporary `HOME`, unsetting `GEMINI_API_KEY`, `GOOGLE_API_KEY`, `LD_LIBRARY_PATH`, under `QT_QPA_PLATFORM=offscreen` with controlled timeout (`timeout 5s ... || [ $? -eq 124 ]`). Verify `$tmp_home/.local/share/applications/falafacil.desktop` exists, mode is `0644`, `Exec`/`TryExec` point to stable Homebrew opt path `$(brew --prefix)/opt/falafacil/bin/falafacil`, and `command -v falafacil` resolves to Homebrew bin.
5. On subsequent releases, verify in-app update (**Configurações → Atualizações → Instalar atualizações**).
6. Verify local `main` is clean and synchronized with `origin/main`: `git status --porcelain` is empty and `[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ]`.
7. Output comprehensive release report: version/summary/tag, commit hash, CI run ID/URL/conclusion, release URL/immutable state/assets/calculated SHA-256, tap commit/formula SHA-256, Homebrew installation evidence, and clean git status with verified HEAD == origin/main SHA.
