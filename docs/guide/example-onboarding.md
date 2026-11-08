# Example: prepare a project and submit a first request

This example uses `example-project` and a local repository as placeholders. It illustrates the configuration and request sequence; the integration has not been verified end to end. A project ID in `ax work` does not itself register a repository or provision an execution route.

1. Complete [getting started](getting-started.md), configure an authorized intake identity, and prepare a Git repository accessible to the selected subloop. For example, place a bare repository at `$HOME/ax-example/example-project.git`. Grant the runtime only the access needed for the intended work; keep credentials outside Git.
2. Register the repository with the subloop that will use it. The runtime reads `AX_REPO_*` variables and maps underscores in the suffix to hyphens. For the existing `sl-work-tdd` service, copy the [project template](../../templates/project/README.md). Its illustrative Compose override is:

   ```yaml
   services:
     sl-work-tdd:
       environment:
         AX_REPO_EXAMPLE_PROJECT: /git/example-project.git
       volumes:
         - type: bind
           source: ${EXAMPLE_REPO_PATH}
           target: /git/example-project.git
           read_only: true
   ```

   Set `EXAMPLE_REPO_PATH` to the absolute path of the bare repository when applying the override, and confirm the runtime user can read it. Apply the override through your deployment's Compose configuration. The stock service only configures `AX_REPO_AX`; exporting a variable solely in the host shell does not add it to that service's environment. Configure the model registry, worker permissions, and any required environment separately using [configuration examples](templates.md). An SSH environment currently supports only `audit-readonly`, not general worker execution.
3. Review the graph and destination registration, the seat authorization, and the configured model and execution environment. The repository mount by itself does not create a graph. Confirm that your configured intake and subloop route the project to a `git.worktree` node with repo `example-project`, a valid base ref and branch. The node resolves the registered repository; the work request alone does not establish this routing. If the route is absent or unverified, stop here and do not treat registration as a working project execution path.
4. Trace the implementation route before submitting. The `work.request` destination validates the payload, creates a ToDo, and sends an item to `work-tdd` through control. For `impl` and `impl_hard`, the item gets `base_ref: main` and branch `ai/<item_key>`. `inbox.take` supplies the item to `work-tdd`; its `tdd-change` include passes `item.project`, `item.base_ref`, and `item.branch` to `git.worktree`. The project ID must resolve through `AX_REPO_EXAMPLE_PROJECT` to the mounted repository, and its `main` ref must exist. The runtime clones or fetches into its writable work area and creates a separate worktree. The test design, implementation, verification and review nodes use that worktree.

   `work-tdd` then sends its result to `work.git` using `AX_REMOTE_WORK_GIT`. The stock overlay mounts a separate writable staging repository. The push uses force for the item branch; isolate this staging repository, inspect permissions, and do not map it to the source repository or a shared production branch. Verify the writable work and staging mounts, plugin and control credentials, worker-pool availability, registered model and runner, seat authorization and the deployed graph before requesting implementation. The `summary`, `research`, `second_opinion`, `screen` and `assist` roles instead submit directly to worker-pool and do not establish this worktree route.

   Inspect the configured Compose service and repository permissions in the target environment. The override is not enabled merely by setting the variable in the host shell. The supplied stock host startup path does not document a general arbitrary-override switch; integrate the override using your deployment's Compose configuration and confirm the resulting service environment before use. This route has not been tested end to end here.
5. Once the route and permissions are ready, submit a scoped first implementation request:

   ```sh
   ax work --project example-project --title "Improve documentation" \
     --role impl --body "Update the project introduction." \
     --example "Describe the project purpose" \
     --boundary "Do not modify application code" \
     --verify "git status --short"
   ax status
   ax todos --project example-project
   ax events --project example-project
   ```

6. Retain the request ID and inspect the response, ToDo and events for acceptance, execution, and verification. Acceptance alone does not establish completion. If the result is unknown, follow the [CLI recovery instructions](cli.md#when-the-result-is-unknown); do not invent a new request ID before reconciliation.

The verification command above is only a Git working-tree inspection; it is not a project test. Replace it with the project's independently reviewed test command. Acceptance, queued work, a created branch and a successful verification are distinct states; inspect the ToDo, events, job result and staging branch before claiming completion. Do not retry an uncertain write with a new ID.
