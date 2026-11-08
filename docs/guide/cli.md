# CLI reference

The CLI builds requests for the configured intake entry. A command being implemented does not guarantee that the destination, permissions, or downstream execution are supported. Run `ax --help` and `ax <command> --help` for arguments. Common options include `--request-id`, `--session`, `--format`, `--json`, and `--lease`. `--agent` and `--model` are accepted for `brief` and `claim` as declarations sent with the authenticated seat; other operations currently reject them.

| Command | Current CLI behavior |
| --- | --- |
| `status`, `todos`, `events` | Read status, ToDos, or events. `todos` and `events` accept filters including `--project`. |
| `env probe` | Probe the specified environment host. |
| `work` | Submit a work request. |
| `update` | Submit an update request. |
| `decide`, `handoff` | Record a decision or handoff through intake. |
| `done`, `park` | Request a ToDo transition. |
| `claim` | Build a claim request via its dedicated handler, despite its presence in the deferred parser list. |
| `seat revoke` | Request revocation of a seat. |
| `answer` | Submit an answer with a required quote. |
| `control` | Request a control operation with a reason and optional JSON arguments. |
| `subloop create`, `core change` | Read a JSON object from the file named by `--spec`, or standard input with `--spec -`, and build a change request. Downstream handling depends on the destination. |
| `cancel`, `stop` | Request cancellation or stopping by `--todo-id` or `--job-id`; cancellation requires `--reason`. A request does not prove a running job stopped. |
| `brief` | Read a session overview; use `--short`, `--section`, and `--offset` to limit or page through it. |
| `whoami`, `guide`, `show`, `approvals`, `approval`, `seats`, `decisions`, `destinations`, `resume`, `change`, `go`, `permission`, `gate` | See the command help and destination availability. |

For `ax work --role research`, provide `--project`, `--title`, a nonempty `--body`, a registered `--target` ID, and `--revision` as a full lowercase Git commit ID (40 or 64 hexadecimal characters). Repeat `--attachment` for registered material IDs, up to three distinct IDs. The target and each material must be registered for the requested project; each material registration needs a nonempty `files` list and `project`. Paths are not accepted as IDs. `--verify` and acceptance fields are not accepted for research.

`ax brief --new-session` reports unavailable sources and partial results. Unknown attributes of older claims remain `unknown`; claims with an unknown project are omitted from a project-filtered result, which is marked partial. Claim agent and model come from declarations submitted with the authenticated seat, and project is recorded only when verified. A claim is considered old after the default lease duration.

`--gate` is a separate SSH entry mode, not the deferred `gate` subcommand. Access and authorization depend on the deployment.

## When the result is unknown

Exit code 5 means the result is unknown; do not assume the operation failed or submit a new request with a different ID. Keep the returned `request_id` and inspect `ax status`, `ax todos --project example-project`, and `ax events --project example-project`, using the actual project identifier. `ax show` is not implemented even if the CLI suggests it. Check the relevant service logs and records for the request or job, without exposing secrets. A retry of a write must reuse its original `--request-id`; first reconcile the actual outcome.

For an environment job, an authorized client can query `GET /v1/env/jobs/<job_id>`. On the environment host, the minimal module stores its record at `$AX_ENV_HOME/jobs/<job_id>/state.json` (by default, the environment home is derived from the launcher installation path, normally `$HOME/.ax-env`); inspect it only with appropriate access. A missing or unreadable record can yield `unknown`, not proof that no job ran. Reconcile remote state and retained capacity before retrying. See [operations](operating.md#environment-jobs) and [known limitations](known-limitations.md).
