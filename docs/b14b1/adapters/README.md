# Common environment adapters

`LocalAdapter`, `SSHHostAdapter`, and `ContainerAdapter` expose the same fixed
S-25.3 job operations: submit, status, logs, fetch, cancel, probe, and lookup.
They delegate only to an injected executor that receives an `ax-env-run`
subcommand and a connection credential reference. No adapter accepts a
credential value or provides a generic command or environment-removal action.

Fetched artifacts are accepted only when their supplied bytes match the
reported size and SHA-256. `collect` scans recovered logs and artifact contents
before returning them. A lost submit response raises `SubmissionUncertain`; the
caller must use `lookup(idempotency_key)` before retrying.

For rental environments, `release_rental` requires complete tracking and zero
job-owned resident processes and containers before asking the runner to move the
job work to `archive/<job_id>`.

## Verification scope

The offline tests cover T-85, T-86, T-88, and the injectable-command portion of
T-116. The real SSH connection, forced-command restriction, source-address
restriction, and host-side permission checks in T-116 remain unexecuted because
this build task does not contact a host or read a real credential.
