---
name: colab-operator
description: Operate Google Colab from coding agents through the `colab` CLI, especially on Windows/PowerShell. Use for persistent CPU/GPU/TPU sessions, one-shot remote jobs, repeated `exec` workflows, file transfer, session recovery, keep-alive, and safe cleanup.
---

# Colab Operator

Use Google Colab as remote compute from an automated coding agent without opening the notebook UI.

This skill assumes the `colab` executable is the CLI from this repository. On Windows, prefer PowerShell-compatible commands and non-interactive CLI paths.

## First: verify the executable

Before allocating anything, verify what the agent will actually run:

```powershell
Get-Command colab
colab version
colab sessions
```

On the Windows fork, `Get-Command colab` should normally resolve to something like:

```text
C:\Users\<user>\.local\bin\colab.exe
```

If working from a checkout of this fork and the installed tool is missing or stale, install the current checkout rather than PyPI:

```powershell
cd C:\path\to\google-colab-cli
uv tool install --reinstall --force .
colab version
```

After pulling or changing the CLI source, repeat that install command before testing through `PATH`. A running agent/terminal may need to be restarted to pick up a newly-added PATH entry.

The package exposes:

```text
colab = colab_cli.cli:main
```

## Authentication

The default CLI authentication provider is `oauth2`.

A previously completed browser authorization is cached under the user's Colab CLI config, so agents can normally reuse it non-interactively afterward.

Read-only verification:

```powershell
colab sessions
```

If the OAuth browser flow is required for the first time, a human must complete it. Do not make an autonomous agent repeatedly retry an interactive auth prompt.

`colab auth` is different: it injects credentials *inside* the remote VM for code that needs GCP access. It does not fix CLI login problems.

## Choose the execution model

### Persistent session: default for multi-step agent work

Use one named session when the agent will run many commands, iterate on code, install packages, or preserve Python state:

```powershell
colab new -s agent
colab exec -s agent -f .\script.py --timeout 300
colab status -s agent
```

A session preserves the remote Jupyter kernel across `exec` calls. Variables, imports, installed packages, and files on the VM remain available until the kernel/session is stopped or replaced.

Prefer a descriptive unique name such as `opencode-quant`, `codex-etl`, or `agent-cv` instead of creating random unnamed sessions.

### One-shot job: use `colab run`

For an isolated script that should allocate, execute, and release automatically:

```powershell
colab run .\job.py
```

GPU example:

```powershell
colab run --gpu T4 .\train.py --epochs 10 --batch-size 32
```

Keep the created session only when continued work is intentional:

```powershell
colab run --gpu T4 --keep -s training-agent .\train.py
```

Without `--keep`, `run` should clean up the allocation even when the script fails.

## Agent operating loop

For multi-step work, follow this loop:

1. Run `colab sessions` before allocating anything.
2. Reuse the requested named session if it is already active.
3. Otherwise create exactly one session with `colab new -s <name>`.
4. Use `colab exec -s <name> -f <local.py>` for normal Python work.
5. Inspect with `colab status -s <name>` and `colab log -s <name>` when something behaves unexpectedly.
6. Stop only the session the agent owns when the task is truly finished.

Never create a replacement runtime merely because one `exec` command times out. First inspect the existing session and retry/recover it.

## Session and runtime recovery

This fork includes runtime-proxy token refresh and console/session recovery from upstream PR #109 plus Windows-specific fixes.

For normal `exec`, file operations, and startup paths, expired runtime-proxy credentials can be refreshed against the same Colab assignment. The CLI should not require a new VM merely because a runtime token expired.

If a command fails:

```powershell
colab sessions
colab status -s agent
colab log -s agent -n 30
```

Then retry the same operation once if the session still exists.

If the kernel is wedged but the VM is still active:

```powershell
colab restart-kernel -s agent
```

Only stop/recreate the VM after confirming the existing assignment is unusable.

Do not implement recovery by blindly doing `colab new` after every timeout; that can lose remote state and leak compute allocations.

## Windows console behavior

Windows does not provide POSIX `termios`, so this fork keeps Windows TTY operation separate from POSIX raw-terminal setup while preserving interactive reconnect semantics.

Automated agents should still avoid unpiped interactive modes:

```text
colab repl
colab console
colab auth
colab drivemount
```

Those can require real terminal/user interaction.

For shell-like batch work, piped console input is allowed:

```powershell
"pwd`nls -la`nexit" | colab console -s agent
```

However, prefer `colab exec` for agents whenever Python can perform the task; it is easier to capture, time out, and recover safely.

## Execution

Run a local Python file remotely:

```powershell
colab exec -s agent -f .\analysis.py --timeout 300
```

Pass environment variables:

```powershell
colab exec -s agent -f .\job.py --env MODE=test --env LIMIT=100
```

The remote working directory is normally `/content`.

For long jobs, set an explicit timeout suitable for the task. A local timeout does not automatically prove that the VM disappeared; inspect the session before recreating it.

## Accelerators

CPU:

```powershell
colab new -s agent
```

GPU:

```powershell
colab new -s agent --gpu T4
```

Supported GPU variants currently exposed by this branch are `T4`, `L4`, `G4`, `H100`, and `A100`.

TPU:

```powershell
colab new -s agent --tpu v6e1
```

Supported TPU variants are `v5e1` and `v6e1`.

Accelerator availability depends on the user's Colab entitlement and current capacity. Do not spin repeatedly through expensive accelerator requests after quota/capacity failures.

## Files and environment

Useful commands:

```powershell
colab ls -s agent
colab upload -s agent .\data.parquet /content/data.parquet
colab download -s agent /content/result.parquet .\result.parquet
colab rm -s agent /content/tmp.bin
colab install -s agent numpy pandas pyarrow
```

This Windows fork preserves its compressed/chunked transfer paths while wrapping runtime access in refreshed-session retry logic.

## Google Drive mounting

`colab drivemount -s <name>` uses Colab DriveFS. The first Drive mount on each fresh Colab VM requires Google OAuth consent from a human; agents cannot safely bypass that consent screen.

Once Drive has been authorized and mounted on that live VM, this fork pre-checks the mountpoint and repeated `colab drivemount` calls return immediately without another OAuth prompt. Reuse the same mounted session for agent work instead of remounting on a new VM.

For fully unattended checkpointing, prefer `colab download`/`colab upload` or an explicitly configured non-interactive durable store. Do not make an autonomous agent block on a fresh-runtime Drive consent flow.

## Keep-alive

`colab new` starts a detached keep-alive helper automatically. The agent should not launch a second keep-alive process manually.

The keep-alive implementation uses the Colab Tunnel Frontend assignment ping on `colab.research.google.com` and periodically performs kernel activity so headless sessions are less likely to be idle-pruned.

`colab stop -s <name>` terminates the session and its keep-alive process. On Windows, the stored launcher PID and the Python daemon PID can differ; lifecycle tests verify that stopping the session reaps the process chain.

Keep-alive is not persistence. Remote state can still disappear because of Colab policy, runtime limits, account limits, backend resets, or other service-side conditions. Important work must be checkpointed to durable storage.

## Durable checkpointing

Treat `/content` and in-memory kernel state as ephemeral. A healthy keep-alive process reduces idle pruning but cannot guarantee that Colab will preserve the VM.

For work that would be costly to repeat, checkpoint at meaningful milestones and before risky operations such as dependency upgrades, kernel restarts, long unattended waits, or major pipeline stages.

Prefer durable outputs over reconstructing state from a live notebook kernel:

- Keep source code and configuration in the local project or version control; do not make the Colab VM the only copy.
- Write resumable artifacts on the VM, such as model checkpoints, intermediate Parquet files, manifests, progress JSON, seeds, and completed-partition markers.
- After important milestones, copy irreplaceable remote artifacts back to the local project with `colab download`, or sync them from the remote program to an explicitly configured durable store such as Google Drive, GCS, Hugging Face, or another user-approved destination.
- For long training or data-processing jobs, design the script to resume from its latest checkpoint instead of assuming one uninterrupted Colab lifetime.
- Record enough metadata to reproduce or resume the job: input/version identifiers, parameters, random seed, completed ranges/partitions, checkpoint path, and the last successful stage.

Example local checkpoint retrieval:

```powershell
colab download -s agent /content/checkpoints/latest.pt .\checkpoints\latest.pt
colab download -s agent /content/progress.json .\checkpoints\progress.json
```

Do not stop or restart a session containing uncheckpointed work unless recovery has failed or the user explicitly accepts losing that state.

## Parallel agents

Avoid having unrelated agents mutate the same session-state file and session name.

Give each agent its own session name. For stronger isolation, use a separate config path:

```powershell
colab --config "$env:TEMP\colab-opencode.json" new -s opencode
colab --config "$env:TEMP\colab-opencode.json" exec -s opencode -f .\job.py --timeout 300
colab --config "$env:TEMP\colab-opencode.json" stop -s opencode
```

The keep-alive child inherits the selected `--config` and authentication mode.

## Cleanup safety

Before and after live work:

```powershell
colab sessions
```

Stop sessions the agent explicitly created:

```powershell
colab stop -s agent
```

A server-side assignment shown as `[?]` has no matching local session record. **Do not kill or unassign an unknown `[?]` assignment automatically.** It may belong to the user, another terminal, or another agent. Ask or establish ownership first.

Never leave a known test/agent allocation running after the work is complete unless the user explicitly asked to keep it.

## Recommended agent policy

When this skill is active, follow these defaults:

- Prefer `colab exec` with a persistent named session for iterative agent work.
- Prefer `colab run` for independent one-shot scripts.
- Check `colab sessions` before allocation and after cleanup.
- Reuse a healthy named runtime instead of creating duplicates.
- Treat timeouts as a diagnostic event, not proof that the runtime is gone.
- Let the CLI's token/session recovery try to preserve the same assignment.
- Checkpoint costly or irreplaceable work to durable storage at meaningful milestones; never treat `/content` or kernel memory as the only copy.
- Make long jobs resumable from their latest checkpoint whenever practical.
- Never use interactive `repl`, `console`, or `auth` from a non-interactive agent unless input is intentionally piped and supported.
- For Drive, reuse an already-mounted live session. A fresh VM's first `drivemount` requires human OAuth consent; unattended agents should use `colab upload/download` or another configured durable store instead.
- Never terminate an unknown `[?]` assignment.
- Always stop allocations the agent owns when finished.

## Self-check

An agent can confirm this skill is the one bundled with its installed CLI by running:

```powershell
colab skill
```

For this fork, also verify `colab version` matches the expected git-derived build before relying on Windows/session-recovery behavior.
