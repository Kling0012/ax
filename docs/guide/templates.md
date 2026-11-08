# Configuration and request examples

These examples illustrate the integration branch, not a verified production deployment. Values are examples; arrange authorization, network isolation, and secret delivery for the actual installation. A template described as incomplete must not be treated as a deployable manifest.

## Launcher and service environment variables

The placeholders in [launcher.env.example](../../config/launcher.env.example) show the variable names; supply each variable to the process that reads it. The example file does not automatically configure the other services.

| Name | Reader | Default | Behavior when unset |
| --- | --- | --- | --- |
| `AX_LAUNCHER_HOST` | Worker pool launcher SSH client | None | SSH launch fails with `missing_config`. |
| `AX_LAUNCHER_USER` | Worker pool launcher SSH client | None | SSH launch fails with `missing_config`. |
| `AX_MEMCONN_USER` | Memory connector | `operator` | Memory operations use `operator` as the user scope. |
| `AX_WORKER_LOCAL_ENV_ID` | Worker pool | None | No local environment receives preference when placement is `any`. |
| `AX_LOCAL_PRINCIPAL` | Intake service | `local-operator` | Local requests use the default principal. |
| `AX_RETIREMENT_ARCHIVE_NAME` | Retirement filesystem helper on each executing host | `issue-automation-old` | The helper uses that archive subdirectory. |

For the memory connector and the retirement helper, an explicitly empty value is different from an unset variable: it overrides the default with an empty string. The local intake principal falls back to its default when empty. Set the retirement name on every host running the helper, including remote Python processes invoked over SSH, and keep it aligned with the retirement engine's archive subdirectory. Set the worker local environment ID to a registered ID to enable its local preference.

These settings describe changes pending integration. Intake vias and seat both use `AX_LOCAL_PRINCIPAL` for the local principal, including identity reconstruction and lockdown cleanup.

## Embeddings: registry and request guard

The resource service seeds its model registry from `AX_MODELS_FILE` when its resources table is empty. An existing nonempty table is not reseeded by changing that file. The following is an example of the relevant model record, not a migration procedure for an existing registry:

```yaml
models:
  - id: example-provider/example-model
    kind: model
    family: example-provider
    tier: standard
    status: active
    embedding_endpoint: https://example.com/v1/embeddings
```

```text
AX_MODELS_FILE=config/models.example.yaml
AX_EMBEDDING_PROVIDER=example-provider
AX_EMBEDDING_MODEL=example-model
AX_EMBEDDING_URL=https://example.com/v1/embeddings
AX_EMBEDDING_DIMENSIONS=1536
AX_EMBEDDING_TOKEN_FILE=/run/embedding/token
AX_REDACT_INDEX=/run/config/redact-index
AX_PII_LIST=/run/config/pii-list
```

Set dimensions to the actual model output; 1536 is an example. Supply the token and redaction inputs as managed files. The embedding adapter checks the provider, model and endpoint against the registry and applies input guards before an outgoing request. It fails closed if the registry or guarded inputs are unavailable. Queries must use the same model as the indexed vectors. Before enabling a timer, check the configured endpoint against the registered record and confirm that the relevant files are readable. The exact production network and secret configuration is deployment-specific.

## Work control: authenticated cancellation

The registered intake destinations are `work.cancel` and `work.stop`. An authenticated principal needs permission to operate on the target. For example, send the following body to `POST /v1/instructions` through the configured authenticated intake entry point:

```json
{
  "from": "commander",
  "destination": "work.cancel",
  "idempotency_key": "<YOUR_IDEMPOTENCY_KEY>",
  "payload": {"job_id": "example-job", "reason": "operator request"}
}
```

Use a fresh valid idempotency key for each new instruction and replace the job ID with the actual target. `todo_id` can identify the target instead. The authenticated identity is established by the entry point, not by the `from` field. A queued job can be cancelled promptly; for a running job, the response can be `stop_requested` while its state remains `running`. Check the job state and ledger history until the outcome is known. Requests for other owners or insufficient privileges are denied. `work.resume` remains **not implemented**; do not use cancellation as a pause/resume mechanism. A complete entry-point authentication deployment template has not been verified.

## Conversation indexing: read-only host inputs

Configure both host paths together; the host startup script mounts them read-only and sets container paths. For example:

```sh
AX_CONVERSATION_HISTORY_HOST_DIR="$HOME/ax-data/conversations"
AX_EMBEDDING_TOKEN_HOST_FILE="$HOME/ax-secrets/embedding-token"
AX_EMBEDDING_PROVIDER=example-provider
AX_EMBEDDING_MODEL=example-model
AX_EMBEDDING_URL=https://example.com/v1/embeddings
AX_EMBEDDING_DIMENSIONS=1536
```

These are configuration assignments, not instructions to create a token with an example value. Prepare existing readable files through your management system, configure the embedding registry and guards above, then start the optional conversation-index timer. Inspect the timer result and service logs for missing inputs or registry rejection. The runtime paths are `AX_CONVERSATION_HISTORY_DIR=/conversation-history` and `AX_EMBEDDING_TOKEN_FILE=/run/embedding/token` when the startup mount is used. An unavailable index or endpoint affects indexing; it is not evidence that unrelated timers must stop. Live deployment and input permissions remain unverified.

## Environment jobs: submit and inspect

The following request body matches the implemented audit job interface. Send it to `POST /v1/env/jobs` using the bearer credential associated with a plugin manifest that allows `submit`, `get`, the requested capability, and entry point. Use the same authorized identity for `GET /v1/env/jobs/example-job`.

```json
{
  "job_id": "example-job",
  "requires": ["security-audit"],
  "scope": "each",
  "sensitivity": "private",
  "entry": {"kind": "entrypoint", "name": "audit-readonly", "args": {}},
  "secrets": [],
  "outputs": []
}
```

Set `AX_ENVIRONMENTS_FILE=config/environments.example.yaml` to select an environment registry relative to the configured AX root; replace the example registry with one reviewed for the deployment. Provision plugin credentials and environment connection credentials separately. A successful submission can return 202; poll the job and inspect the per-environment results. Duplicate job IDs can return 409; missing authentication returns 401 and unauthorized operations return 403. A lost remote response can produce an unknown result and retain capacity while reconciliation checks the outcome. Do not resubmit an uncertain job as a new job without determining what ran. The job API implementation currently exposes submission and status; do not assume the planned logs, artifacts, or cancel endpoints are available through this implementation.

## Remote execution: SSH audit

The SSH adapter uses an environment registry entry and a trusted connection credential. This excerpt illustrates the connection fields used by the adapter; it is not a complete environment registry:

```yaml
id: host-a
adapter: ssh-host
connect:
  kind: ssh
  host: host-a.example.com
  user: ax-runner
  port: 22
conn_credential:
  slot: env-ssh-host-a
  place: secrets_dir/env-ssh-host-a
capabilities: [security-audit]
data_classes: [private]
max_concurrent: 1
entrypoints: [audit-readonly]
```

Provision the authorized SSH identity and host-key trust through the deployment's trusted setup, install the matching environment module, and validate the audit on the intended host. Use the environment job request above and inspect the result for each host. The implemented remote module accepts the `audit-readonly` entry point; general manifest-defined SSH entry points are **not implemented**. If noninteractive privilege escalation fails, the audit reports missing coverage and failure; it is not a successful complete audit. Transport loss makes the outcome uncertain. Complete registry fields, artifact handling, and production host setup require verification.

## Image pinning: update workflow

`runtime/images/base-images.lock` records digest references for several runtime images, and their Dockerfiles use matching default build arguments. The core Dockerfile and CI Dockerfile instead default to `python:3.12-slim`; compose passes `AX_PYTHON_REF` to the core build. The CI build script constructs pinned Python and Docker CLI references from `ci/base-image.lock` and `ci/docker-cli.lock`.

```text
AX_PYTHON_REF=python:3.12-slim@sha256:<verified-64-hex-digest>
PYTHON_REF=python:3.12-slim@sha256:<verified-64-hex-digest>
```

The second line illustrates the `PYTHON_REF` Docker build argument; it is not automatically an environment setting for every build. Resolve and independently verify each image digest, update the relevant lock and Dockerfile defaults or build arguments together, rebuild, and check the resulting image before rollout. The placeholder digest is intentionally unusable. Pinning a base image does not by itself pin every package downloaded during a build. Image builds and deployment were not verified for this draft.

## HQ identity: proof provisioning

The HQ startup path prepares a shared proof key and intake directory when its prerequisites are available. The key path used by the proof verifier can be set as follows:

```text
AX_HQ_PROOF_KEY_FILE=/run/secrets/hq-proof-key
```

Provision the same protected key to the relevant trusted services using the deployment's secret mechanism. The host setup creates a key only if one is absent and checks ownership and permissions; avoid replacing an existing key during normal startup. The proof binds the request method, path, body and identity and expires after 60 seconds; replay is rejected. Check privileged actions through the actual authenticated HQ entry point and investigate key or proof errors rather than bypassing them. If the key is unavailable, affected actions fail closed. The production second-factor notification adapter is **not implemented**; end-to-end provisioning and delivery are unverified.

## Launch slots: capacity and reconciliation

An authorized control client can set a cap with `PUT /v1/slots/cap`. These bodies are examples:

```json
{"kind": "model", "key": "example-provider/example-model", "cap": 2}
```

```json
{"kind": "family", "key": "example-provider", "cap": 3}
```

Reserve with `POST /v1/slots/reserve` and a unique `reservation_id` and `model`; release with `POST /v1/slots/release` and the `reservation_id` after a confirmed terminal result. The slot endpoints require their appropriate service credentials; set caps only through an authorized control client. Inspect `GET /v1/slots` and reconcile uncertain outcomes before releasing a reservation. The model default is one and an unset family cap is unlimited; zero disallows new reservations. Launch reservation is atomic, but complete control-to-cap wiring and the aggregate free-tier cap remain **not implemented**. This example is not an end-to-end capacity deployment.

## Sandbox execution: supported scope

The sandbox tool mapping is in `config/sandbox-tools.yaml`; the environment registry describes the trusted provider and credentials. A policy review should verify the requested capabilities, provider mappings, and admission conditions before granting access. Existing Instabox machines can be used for supported execution; machine creation and deletion are **not implemented** by this integration. Cancellation or timeout of the local process does not prove remote execution has stopped. Inspect the provider outcome before retrying. Invalid policy configuration disables affected routes. `config/environments.sandbox.example.yaml` is an example requiring integration and deployment review; a complete verified managed provider and policy template is not yet available, and sharing capacity arbitration with environment jobs remains unverified.

## Publication review: managed inputs

The host startup script requires all four host inputs to generate the optional read-only mounts. Set each input in the process environment or in `$AX_HOME/config/launcher.env`; an explicitly set process variable takes precedence. In `launcher.env`, use literal absolute paths, optionally enclosed in single or double quotes. Shell expansion, escapes, duplicate keys, and multiline values are not supported. Invalid input disables these mounts while other services continue starting. Unknown keys are ignored. These settings also apply when hostwatch starts the service. This example identifies files and a directory, not their contents:

```sh
AX_PUBLISH_REPOSITORIES_HOST_FILE="/home/example/ax-config/publish-repositories.yaml"
AX_PUBLISH_REPOSITORY_HOST_DIR="/home/example/ax-data/public-repositories"
AX_PUBLISH_HOSTS_HOST_FILE="/home/example/ax-config/internal-hosts"
AX_PUBLISH_TERMS_HOST_FILE="/home/example/ax-config/internal-terms"
AX_MODEL_GATEWAY=https://example.com/model-gateway
AX_PUBLISH_SLOTS_URL=http://resources-review:8080
```

The host mount supplies the corresponding `AX_PUBLISH_REPOSITORIES_FILE`, `AX_PUBLISH_HOSTS_FILE`, and `AX_PUBLISH_TERMS_FILE` container paths. Prepare an allowlist using the shape above, mount the reviewed repository contents, provision the scanner inputs and dedicated service credentials, and validate gateway and slot access. Missing required scanning or review inputs fail closed. Review is not the same as publication: actual publication remains **disabled**. The production review path, credentials, and complete managed deployment remain unverified.

## Change verification recipes

Set `AX_CHANGE_RECIPES_HOST_FILE` in the process environment or `$AX_HOME/config/launcher.env` to an absolute path to a regular, non-symlink file. When `sl-change` starts, the host mounts it read-only at `/ax/plugins/subloop/change/verification/recipes.yaml`. An unset value keeps the bundled recipes; an invalid value prevents their use. The file must contain JSON, which is a subset of YAML, as required by the recipe loader.

For a recipe with dependency commands, configure the trusted target's `outbound_allowlist` and a matching host-side recipe for the same project, target, version, and allowed hosts. Specify the dependency output directories needed by verification in `dependency_artifacts`. Dependencies run with the approved host allowlist; verification runs without network access against a fresh source checkout and the selected dependency outputs mounted read-only. The source integration has limited tests with a real Docker dependency and synthetic output; the production deployment and full workflow remain unverified.

## Bringing an environment under management

A registry entry or manifest does not provision a host or enable an unimplemented entry point. See the [environment manual](../manual/environments.md) for module installation and transport. See [environment module operations](operating.md#environment-modules) for the current installer and limits.

The repository includes three environment-specific examples: `config/environments.example.yaml` for SSH hosts, `config/environments.sandbox.example.yaml` for sandbox adapters, and `config/models.example.yaml` for an initially empty model registry. Review and adapt them; they are not ready-made production deployments. The profile template is `templates/profile/profile.template.yaml`, and `templates/env/environment.template.yaml` describes an environment module's manifest. Keep connection credentials in the configured secret store, not in these files.

A reduced SSH registry entry, using documentation-only addresses and illustrative credentials, is:

```yaml
contract_supported: "1.0"
environments:
  - id: example-env
    adapter: ssh-host
    connect: {kind: ssh, host: "192.0.2.10", user: example-user, port: 22}
    conn_credential: {slot: env-ssh-example, place: secrets_dir/env-ssh-example}
    os: linux
    capabilities: [security-audit]
    entrypoints:
      audit-readonly: {run: entrypoints/bin/audit-readonly}
    data_classes: [public]
    secrets_allowed: false
    work_credentials: []
    secret_targets: []
    max_concurrent: 1
    cost: {kind: fixed}
    lifetime: permanent
    owner_use: false
    rank: 100
    registered_at: "2026-01-01"
    state: trial
```

Set the profile's `environments_file` to the reviewed registry path relative to the AX root. Match the environment ID, contract, capabilities, data classes, and capacity with the target module's manifest. The minimal SSH support is limited to `audit-readonly`; declaring another entry point does not make it executable. Provision SSH identity and host-key trust separately, and verify access and contract compatibility before using the environment for a job.

A corresponding manifest starts from the repository template:

```yaml
env_id: example-env
module_version: 1.0.0
contract: "1.0"
kind: ssh-host
transport: ssh
os: linux
capabilities: [security-audit]
entrypoints: [audit-readonly]
data_classes: [public]
secrets_allowed: false
work_credentials: []
secret_targets: []
max_concurrent: 1
cost: fixed
lifetime: permanent
owner_use: false
model_family: []
constraints:
  no_resident: false
  archive_dir: archive/
```

The manifest is not itself a registry entry or a deployment command. The template's module version must agree with the installed `VERSION`, and its contract with `CONTRACT`. See `templates/env/README.md` for the installer and forced-command templates. The sandbox example has separate connection and gate credential slots, policy settings, and free-tier guards; do not copy real credentials into it. Sandbox policy and adapter connectivity must be reviewed before use.

For models, the checked-in example deliberately starts empty:

```yaml
models: []
```

Register actual models against the deployment's gateway and configure capacity through an authorized resource client. An empty file does not register a model. `config/roles.yaml`, `config/runners/`, `config/intake-destinations.d/`, `config/sandbox-tools.yaml`, and `config/sandbox-entry-steps.yaml` provide role, runner, intake, and sandbox settings; inspect their schemas and implementation before changing them. `config/defaults.yaml`, `config/error-rules.yaml`, and `config/standing-rules.md` hold defaults and policy inputs. They are separate from the profile and environment manifest.


## Model registration

Start with the empty `config/models.example.yaml` and select actual IDs from the current gateway's `/v1/models`. Supply the reviewed file through `AX_MODELS_FILE` to resources; its default is `config/models.yaml`, not the example. A model entry requires `id`, `kind: model`, `family`, `tier`, and `status`; use `level: L1` for a new registration and review `cap` and family limits. See [model fields](../manual/configuration.md#モデル資源) and `schemas/resource.schema.json`. Secrets belong in the deployment's secret store.

Resources seeds the database only while the entire resources table is empty. Editing the file does not update an existing table. Check the resources health and existing entries through the authorized service interface before changing registration; do not assume a restart applies file changes. Gateway discovery, credentials and model execution must be verified in the target deployment.
