# Operations guide

This guide describes the integration state. Review behavior against the release and validate the deployment before use. Examples use placeholders and are not complete deployment manifests.

## Embeddings

Conversation indexing and similarity search use registered embedding endpoints. Register the intended provider and model with the resource service before enabling indexing. Search inputs are checked before submission, and vectors from different models are not mixed. Configure `AX_EMBEDDING_PROVIDER`, `AX_EMBEDDING_MODEL`, and the token file described below. Missing or unregistered endpoints stop indexing rather than other timers. Confirm the registry's current schema before creating a managed registration; a complete registration template is pending.

## Work control

Authenticated work-control entry points record cancellation and support the registered work-control operations. The work.resume destination is not implemented. Cancelling a queued worker job removes it from the queue promptly. A cancellation request is not proof that an already running launcher has terminated. Consult the current CLI and intake API reference for request syntax and authorization; a complete managed request template and live verification are pending.

## Conversation indexing

Provide both `AX_CONVERSATION_HISTORY_HOST_DIR` and `AX_EMBEDDING_TOKEN_HOST_FILE` to enable the optional read-only host mounts. The runtime receives the history through `AX_CONVERSATION_HISTORY_DIR` and the token through `AX_EMBEDDING_TOKEN_FILE`. Set `AX_EMBEDDING_PROVIDER` and `AX_EMBEDDING_MODEL` to the registered values. Supply existing paths through your secret and configuration manager; do not put credentials in configuration files. Input permissions, production paths, and live endpoint settings require verification.

```text
AX_CONVERSATION_HISTORY_HOST_DIR=<managed-history-directory>
AX_EMBEDDING_TOKEN_HOST_FILE=<managed-token-file>
AX_EMBEDDING_PROVIDER=<registered-provider>
AX_EMBEDDING_MODEL=<registered-model>
```

## Environment jobs

Authorized clients submit environment jobs to `POST /v1/env/jobs` and query `GET /v1/env/jobs/{job_id}`. Configure the environment registry via `AX_ENVIRONMENTS_FILE` where applicable and provision the appropriate credentials through the deployment's secret manager. A job with an uncertain remote result keeps its reservation until its outcome is reconciled. The complete request schema and a verified production configuration template remain to be documented.

## Remote execution

The SSH transport supports the `audit-readonly` entry point and returns audit artifacts. It does not yet provide general manifest-defined entry points. A failed privilege escalation reports unavailable coverage and a failed audit. Provision verified host identity and credentials through the deployment's trusted configuration and test the actual remote path before use. A complete production SSH template remains pending.

## Runtime image pinning

Several runtime base images are referenced by digest, but the core and CI Dockerfile defaults use mutable tags unless build arguments override them. When updating a base image, select and independently verify the intended image and digest, update the relevant build configuration, then rebuild and validate the resulting image. A digest reference has the form `image-name@sha256:<verified-digest>`; this is a format example, not a usable image. Image builds and publication were not verified as part of this integration.

## HQ identity

The Compose HQ service has no host TCP port. `compose.d/b10b.yaml` and `compose.d/b12b2.yaml` mount the HQ socket directory, the secret input directory, and the shared HQ-to-intake socket directory. Configure `AX_HQ_UID`, `AX_HQ_GID`, `AX_STATE_DIR`, `AX_SECRETS_DIR`, and `AX_PUBLIC_PROXY_IP` for the deployment. Host Tailscale Serve connects to the HQ Unix socket. Only a trusted proxy may supply the single `X-Forwarded-For` value; restrict access to the socket and configure the proxy address correctly. HQ classifies the source and applies the corresponding identity verification; public access additionally requires the configured Cloudflare Access issuer, audience, and allowed users. Never trust forwarded headers from arbitrary clients. Validate the actual proxy and socket permissions in the deployment.

Privileged approval and authorization changes require signed, short-lived identity proofs. Proofs are validated against the request and protected against replay. If the proof key is unavailable, affected actions fail closed; ordinary operations continue. Provision the signing key and shared intake directory with the deployment's host setup, without replacing an existing key. The production second-factor notification adapter is not connected, and live identity and delivery have not been verified. A complete deployment template remains pending.

## Launch slots

The resource service supports model and family capacity limits and atomic reservations. A missing model cap defaults to one; an unset family cap is unlimited; a cap of zero prevents new reservations. Launches reserve before starting, and confirmed terminal outcomes release reservations. An uncertain outcome retains its reservation until reconciled. Control-to-cap wiring and the aggregate free-tier cap remain incomplete; do not assume every control surface enforces the same limit. A complete managed cap template remains pending.

## Sandbox execution

Sandbox routes apply job policy and admission checks before transport. Instabox execution uses an existing sandbox; sandbox creation and deletion are not supported. Timeout and cancellation stop the local process, but an uncertain remote outcome remains uncertain. Configure a trusted provider and policy explicitly; invalid sandbox configuration disables the affected routes. Shared capacity arbitration with environment jobs and live deployment are not yet verified. A complete policy template remains pending.

## Publication review

Publication scanning checks configured sensitive content before review. Missing required inputs keep the gate closed, and actual publication remains disabled. The release configuration accepts `AX_PUBLISH_REPOSITORIES_HOST_FILE`, `AX_PUBLISH_REPOSITORY_HOST_DIR`, `AX_PUBLISH_HOSTS_HOST_FILE`, and `AX_PUBLISH_TERMS_HOST_FILE` for read-only inputs. Runtime configuration includes `AX_PUBLISH_REPOSITORIES_FILE`, `AX_PUBLISH_HOSTS_FILE`, `AX_PUBLISH_TERMS_FILE`, `AX_MODEL_GATEWAY`, and `AX_PUBLISH_SLOTS_URL`. Supply independently verified repository identities, required scanner inputs, and dedicated credentials through managed configuration. The trusted mounts, gateway, and production review path require deployment validation; a complete production template remains pending.

An allowlist entry uses this shape; verify repository visibility and mount contents before use:

```yaml
repositories:
  - repo: <owner>/<repository>
    path: <read-only-repository-path>
    visibility: PUBLIC
```

## Daily intake and approvals

Use `ax work --project <id> --title <title>` to submit a work request. Optional `--body`, `--role`, `--example`, `--boundary`, and `--verify` describe the task and its acceptance conditions. `ax status`, `ax todos --project <id>`, and `ax events --project <id>` query state. Retrying a write after an uncertain response should reuse its `--request-id`; check the outcome before issuing a new instruction.

Approval is a separate decision from submitting work. Review the destination, scope, and proposed action in HQ, then grant or deny the pending approval there using the authorized identity. The intake destinations are `approval.grant`, `approval.deny`, and `approval.withdraw`; they require the HQ authorization path. The CLI lists `approval` and `approvals` as deferred operations, so do not rely on them as working commands. Production second-factor delivery is not connected; see [limitations](known-limitations.md).

## Resources and environment capacity

The resource ledger is initialized from the configured model registry; its subsequent state is maintained by the resource service. Model reservations default to one when a model cap is absent; an unset family cap is unlimited. Zero stops new reservations and negative limits, including `-1`, are invalid. Existing work is not automatically stopped by setting a cap to zero. The launch-slot API and its incomplete control wiring are described in [capacity examples](templates.md#launch-slots-capacity-and-reconciliation).

Environment entries have their own `max_concurrent` capacity and state. A job may need both model and environment capacity; sandbox and environment jobs do not yet share atomic capacity arbitration. A timeout or lost response can leave a reservation held: inspect the job and reconcile its actual remote outcome before releasing it or submitting a replacement.

## Changes and merge checks

For change-ops, check the pull request number and the exact head SHA against the trusted change record and the current remote PR immediately before merging. If either differs, stop and review the new state rather than merging an earlier approval. Cancellation requires trusted PR identity and the shared ledger; the GitHub adapter alone cannot establish a safe cancellation. Verify whether a merge already occurred before retrying an uncertain operation.

## Environment modules

The [environment manual](../manual/environments.md) describes module installation, relay, and manifest-defined entry points. The current SSH adapter supports only `audit-readonly`; registering a different entry point does not implement it. Existing Instabox sandboxes can be used, but sandbox creation and deletion are not supported. Live SSH and complete deployment have not been verified.

The minimal installer `host/env-module-min/install.sh` installs `releases/b6` under `~/.ax-env`, with `current -> releases/b6` and `bin -> current/bin`. Run it on the target host only after preparing access and reviewing the installer; `--check` inspects an existing installation without writing. It does not install an authorized SSH key and refuses to replace a different current release.

For an installation using a reviewed absolute destination, run `host/env-module-min/install.sh --dest "$HOME/.ax-env"` on the target, then run the same command with `--check`. This checks the B6 installation layout, not end-to-end remote execution. Provision the authorized identity and host-key trust separately.

For a versioned module, keep the prior release under `releases/`. Switching `current` to the selected release changes the fixed path through `bin`; rollback means pointing `current` back to the previously retained release. Check the contract and probe the environment after a change. The minimal B6 installer is not a general upgrade or rollback manager, and remote SSH execution currently supports only `audit-readonly` for environment jobs.

## Logs and state

The profile's `ax_state_dir` holds host deployment state, including `deploy/profile.env`, `deploy/current.env`, and the configured sockets. Compose services also use their own persistent volumes; do not assume every database resides directly in `ax_state_dir`. Use `ax events` and `ax todos` to inspect recorded events and pending work. Docker Compose service logs can help identify startup and dependency failures; avoid sharing credentials or sensitive log contents.

## Secret rotation and recovery

For the secret inventory and rotation procedure, use [the security manual](../manual/security.md#73-秘密の更新). Keep secret values out of Git, terminal transcripts, events, ToDos and backup inventories. Identify each issuer, consumer, file and authentication table first. The host secret directory is selected by `AX_SECRETS_DIR`; the container secret directory is `AX_SECRETS_PATH` when overridden. Core and plugin token digests are stored separately under `AX_STATE_DIR` or `AX_PLUGIN_TOKENS_FILE`. Startup does not automatically reconcile existing mismatched token entries. A coordinated, tested rotation procedure for those entries is not yet available.

For backup and recovery, plan and test the following in an isolated environment before relying on it:

1. Inventory the profile, `AX_STATE_DIR` deployment state, authorized secret store and every persistent Compose volume from the active configuration. For example, timeline, ledger, control, resources and worker-pool use distinct volumes. Preserve matching versions and configuration; state under `AX_STATE_DIR` is not the whole installation.
2. Quiesce the relevant writers using the deployment's approved procedure, or use a database-consistent snapshot method for every SQLite database including its journal or WAL state. Take coordinated copies of the relevant volumes, configuration and required secret material into protected storage. Record the scope and time without recording secret values. A live copy of a database file alone is not a proven consistent backup.
3. Restore a copy into an isolated environment using the matching configuration, volume layout, permissions and secret references. Keep the original backup unchanged and do not connect the restored instance to production dispatch, publication or external side effects. Restore the token tables with their corresponding credentials; do not mix snapshots from different rotations.
4. Start only the components needed for the recovery check under controlled conditions. Check service health, authentication, relevant ToDo and event records, model registrations and pending jobs. Reconcile external operations and reservations before allowing execution. Record any missing state and do not replace production data until the recovery plan is approved.

These steps describe a recovery plan, not a verified automated restore. AX does not provide a documented end-to-end backup and restore command for all volumes in this release. Validate consistency, authorization and isolation for each deployment.

## Troubleshooting

1. Check the startup output, profile path, required fields, secret file names and permissions, Docker/Compose availability, and non-interactive sudo.
2. Check service health and Compose logs, then `ax status`, `ax events`, and relevant ToDo cards. Identify the failing component before restarting it.
3. For a rejected request, check its destination, identity, approval, scope, and schema. For a queued job, inspect model and environment registration, capacity, and exclusions.
4. For an unknown remote outcome, inspect the remote job and reconcile its reservation before retrying. Consult [known limitations](known-limitations.md) and avoid assuming that a simulated integration has been verified live.
