# Managed project template

Copy and adapt `compose.override.example.yaml` for one project. This is a configuration example, not a complete or verified deployment. The [onboarding guide](../../docs/guide/example-onboarding.md) is the primary procedure.

| Field | Example | Purpose |
|---|---|---|
| `sl-work-tdd` | existing service | Subloop that needs repository access |
| `AX_REPO_EXAMPLE_PROJECT` | `/git/example-project.git` | Runtime repository path; suffix underscores become hyphens in the project ID |
| `source` | `${EXAMPLE_REPO_PATH}` | Absolute host path to a repository readable by the service user |
| `target` | `/git/example-project.git` | Container path matching the environment variable |
| `read_only` | `true` | Restricts this source mount to reads |

Configure an authorized intake seat, a working graph route to `git.worktree`, a valid base ref and output branch, worker permissions, models and execution environment separately. A project ID does not create these. Verify the route before submitting the first request; stop if it is absent or unverified. Keep credentials in the deployment's secret store, not this template. The sample has not been verified end to end.

The stock `work-tdd` graph uses the `main` base ref and pushes item branches to the separate `work.git` staging remote with force. Review and isolate that writable remote before enabling implementation. `AX_REPO_EXAMPLE_PROJECT` identifies only the read source; it does not configure the staging remote.
