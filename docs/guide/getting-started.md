# Getting started

AX runs services in Docker Compose and accepts work through an intake service. Some services are running in production and have undergone limited verification; acceptance of AX as a whole remains incomplete. The AXD screen after a legitimate GitHub login has not been verified. Read [known limitations](known-limitations.md) before enabling integrations.

## Prerequisites

Use a Linux host with Docker Engine, Docker Compose, Git, Python 3.12 or newer, and `uv`. The startup script uses non-interactive `sudo` by default. Check `docker compose version`, `python3 --version`, and `uv --version`. Remote environments have their own requirements and credentials.

The repository includes `uv.lock` and a pinned uv version; the CI base and Docker CLI image locks have `status=verified`. These records do not establish that a locked install or image build succeeds in your environment. Do not treat a successful local setup as production validation.

## Obtain and configure AX

Clone the repository using your own repository URL and use the default installation directory:

```sh
git clone <repository-url> "$HOME/.local/share/ax/prod"
cd "$HOME/.local/share/ax/prod"
mkdir -p "$HOME/.config/ax/profile"
cp templates/profile/profile.template.yaml "$HOME/.config/ax/profile/default.yaml"
```

Replace the placeholders in the profile with values for your deployment. `host`, `bind_addr`, `secrets_dir`, `tmp_dir`, `archive_target`, `environments_file`, `memory_endpoint`, `model_gateway`, `models_file`, `ntfy_endpoint`, `ntfy_topic`, `ax_state_dir`, and `commander_socket` are required. Paths to the two registry files are relative to AX's repository root; state, temporary, secrets, and socket paths must be absolute or use `~`. Use `127.0.0.1` for a local bind. The [templates guide](templates.md) explains the registry and environment manifest examples.

Store credentials in files under the profile's `secrets_dir`, outside Git. The repository's [secret inventory](../../templates/secrets/README.md) names the files and their consumers, including `model-gateway-key` and the `core-tokens` and `plugin-tokens` directories. Provide only the credentials needed for enabled services. Do not put secret values in the profile, examples, logs, or requests. The startup script creates some service tokens when missing; it does not provision every external credential.

## Build, start and inspect

Before the first build, prepare a valid profile, Docker Compose, non-interactive `sudo -n`, and the required vendor files. The Git remote named `local` must provide the target `main` and an `accepted/<sha>` tag pointing to that commit. On the first installation, the clone's HEAD must match the target `main`.

The public export contains one root commit on `public/main`, without deployment references. The following procedure tests that exact commit and issues a local acceptance tag only after both fixed tests pass. The tests cover deployment acceptance checks, rollback and first-build retry using fake Git and Docker. The tag records that the issuer ran this test set; it does not guarantee a real Docker build, service startup, integrations, security, or the entire installation.

Set `TMPDIR` to an existing temporary directory, then run this Bash block from the deployment clone. Set `deployment_repo` to a new path whose parent already exists. The block refuses an existing path, an existing `local` remote, an existing `local/main` tracking reference, or an existing acceptance tag in the clone. It also requires a clean clone, with no tracked changes or untracked files. Ignored files are not checked; keep deployment inputs outside the clone. It installs the locked development dependencies into a temporary copy of the committed files, so uncommitted changes are not tested or included. It creates the repository and its two references only after the tests succeed. Keep the temporary directory for inspection if a step fails.

```bash
(
  set -euo pipefail
  source_repo=$(pwd -P)
  deployment_repo="$HOME/.local/share/ax/accepted.git" # choose a new path
  test "$(git rev-parse --show-toplevel)" = "$source_repo"
  test -z "$(git status --porcelain --untracked-files=all)"
  commit=$(git rev-parse --verify 'HEAD^{commit}')
  tag="accepted/$commit"
  if [ -e "$deployment_repo" ] || [ -L "$deployment_repo" ]; then exit 1; fi
  if git remote get-url local >/dev/null 2>&1; then exit 1; fi
  if git show-ref --verify --quiet refs/remotes/local/main; then exit 1; fi
  if git show-ref --verify --quiet "refs/tags/$tag"; then exit 1; fi
  work=$(mktemp -d "${TMPDIR:?Set TMPDIR to an existing temporary directory}/ax-acceptance.XXXXXXXX")
  git archive "$commit" | tar -xf - -C "$work"
  uv sync --locked --group dev --directory "$work"
  PYTHONPATH="$work/host/lib:$work" uv run --locked --no-sync --directory "$work" \
    python -m pytest -q tests/b8/test_t104_deploy.py tests/b8/test_deploy_bootstrap_cli.py
  test "$(git rev-parse --verify 'HEAD^{commit}')" = "$commit"
  test -z "$(git status --porcelain --untracked-files=all)"
  mkdir "$deployment_repo"
  git init --bare "$deployment_repo"
  git -C "$deployment_repo" fetch --no-tags "$source_repo" "$commit"
  printf 'start\ncreate refs/heads/main %s\ncreate refs/tags/%s %s\nprepare\ncommit\n' \
    "$commit" "$tag" "$commit" | git -C "$deployment_repo" update-ref --stdin
  git remote add local "$deployment_repo"
  git fetch --no-tags local main "refs/tags/$tag:refs/tags/$tag"
  test "$(git rev-parse FETCH_HEAD)" = "$commit"
  test "$(git rev-parse "refs/tags/$tag^{commit}")" = "$commit"
  printf 'Accepted commit: %s\n' "$commit"
)
```

If dependency installation or either test fails, stop: no deployment repository or acceptance tag has been created. Resolve the failure and rerun the whole block. A later failure can leave a partially initialized repository; inspect it and choose a different new path before retrying. Review any additional requirements for your environment before using the tag.

From the clone, generate the environment and deploy the images:

```sh
export AX_HOME="$HOME/.local/share/ax/prod"
export AX_PROFILE="$HOME/.config/ax/profile/default.yaml"
"$AX_HOME/host/ax-env.sh" --profile "$AX_PROFILE"
"$AX_HOME/host/ax-deploy.sh"
```

The deploy command builds missing images and can create `current.env` on the first installation. A failed build leaves deployment incomplete and can be retried. The startup script does not build images. This first-installation path has been tested with fake Git and Docker; its real Docker build, deployment, and service startup have not been verified. Some services have been deployed and checked separately in production; acceptance of the whole system remains incomplete.

Use the same profile to start the services:

```sh
host/ax-up.sh --profile "$HOME/.config/ax/profile/default.yaml" observer intake timeline ledger
ax status
```

The script generates deployment environment files, starts the named Compose services, waits for health checks, and registers healthy services for monitoring. It also links `host/ax` into `~/.local/bin` when available; add that directory to `PATH` if necessary. Starting this subset alone does not guarantee that every dependency or integration is ready. If startup or status fails, follow the [troubleshooting order](operating.md#troubleshooting).

## Bring the first project under management

Choose a project identifier and prepare the applicable environment, model registry, permissions, credentials, and verification command for that project. An environment registry entry and its module describe where a job may run; an identifier in a work request alone does not install an environment or establish access. See [configuration examples](templates.md#bringing-an-environment-under-management) and [operations](operating.md#environment-modules).

Submit a scoped first request through intake:

```sh
ax work --project example-project --title "Inspect the project" \
  --body "Inspect the configured project and report findings." \
  --example "Report the result of the inspection" \
  --boundary "Do not modify project files" \
  --verify "git status --short"
ax todos --project example-project
ax events --project example-project
```

These are CLI options, not a promise that the project's execution route is already provisioned. Check the returned request and subsequent events; resolve missing permissions or dependencies before expecting execution. See the [operations guide](operating.md) for approvals, capacity, and failure handling.
