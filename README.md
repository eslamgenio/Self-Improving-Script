# Application Self-Improver

A Python controller that reviews an application's source with Codex CLI, creates
up to three Jira Cloud backlog tickets, implements the highest-priority eligible
ticket in a Git worktree, verifies it, and fast-forwards the local target branch.
It repeats five minutes after each cycle until you press Ctrl+C.

Product growth and user experience guide recommendations. Critical security,
data-loss and correctness defects take priority. Features must fit the documented
application purpose. Ambiguous product decisions block the ticket.

## Prerequisites

- Python **3.12+**. On this Windows machine use `py -3`; the `python` alias is broken.
- Git, with `user.name` and `user.email` configured.
- Codex CLI, authenticated with `codex login`. For the npm installation, Node.js
  must be available. The installed CLI must support `exec --output-schema`,
  `--approve-for-me`, and the read-only sandbox (verified against CLI 0.163.0).
- A local application **Git repository with an initial commit**, a clean working
  tree, and its target branch checked out. Install that application's toolchains.
- Jira **Cloud Software**, a project and board with backlog enabled. The account
  needs Browse Projects, Create Issues, Edit Issues, Transition Issues and
  Schedule Issues, plus access to the board and issue properties.
- An Atlassian account email and API token. Use the site URL for an unscoped token;
  scoped tokens use `https://api.atlassian.com/ex/jira/<cloudId>` as the base URL.

The Python controller has **no third-party runtime dependencies**. Codex usage
uses the account/model configured in your CLI and is subject to its usage limits.

## Set up the SCRUM application on another Windows machine

Copy this tool's whole folder and clone the application repository. Keep its
`main` branch clean and checked out. Have Python 3.12, Git, a signed-in Codex CLI,
and Docker Desktop running in Linux container mode.

From the tool folder, run:

```powershell
.\setup-scrum.ps1
```

The launcher uses the saved application path, Atlassian sign-in email, and API
token automatically. It initializes the SCRUM board 1 mappings on a fresh clone
and reuses that configuration on later runs.

The launcher reads and saves `setup-scrum.json` beside `setup-scrum.ps1`. You can
edit `application_path`, `jira_email`, and `jira_api_token` in the file before
running the launcher. Use an absolute path with forward slashes or doubled
backslashes in JSON. The token is stored in plain text, and `.gitignore` excludes
the file from Git. Keep the file private. If you enter the token at the masked
prompt, the launcher saves it only after `doctor` passes.

If the saved path is blank, the launcher checks the current Git repository and
then the neighboring `Self-Improving App` folder for a matching configuration.
Jira credentials in `setup-scrum.json` take precedence over PowerShell environment
variables. The launcher uses a token with an associated email only when that
email matches the selected account. It prompts for any values still missing
after these fallbacks and restores your previous Jira environment variables when
it exits.

After `doctor` passes and settings are saved, press Enter or choose `1` to run
one cycle. Choose `2` to run continuously and press Ctrl+C to stop, or choose `3`
to exit without starting a cycle.

## Start from an application directory

Keep this tool outside the application you are improving. From PowerShell:

```powershell
$tool = 'C:\Eslam\2- Areas\Projects\Self-Improving Script\self_improve.py'
Set-Location 'C:\path\to\your-application'

py -3 $tool init --app-id my-app `
  --jira-url 'https://YOUR-SITE.atlassian.net' `
  --project APP --board-id 123 `
  --priority-ids '1,2,3,4,5' `
  --bug-type '10004' --improvement-type '10002' --feature-type '10001'
```

**The IDs above are examples, not universal Jira defaults.** Use your instance's
actual IDs, with priorities ordered highest first. `init` clones the official
[agent-skills repository](https://github.com/addyosmani/agent-skills), resolves a
commit, checks out that commit, and saves its hash. Future runs never silently
update the skills. `--skills-revision <sha>` pins an explicit revision;
`--skills-source <local-clone>` supports an audited offline clone.

`init` prints the configuration path. For an ordinary checkout it is
`.git/self-improver/config.json`; linked worktrees share the Git common directory.
This configuration and all runtime state are outside tracked application files.

Inspect/edit it before starting. Important Jira settings:

```json
{
  "base_url": "https://YOUR-SITE.atlassian.net",
  "project": "APP",
  "board_id": 123,
  "issue_types": {"bug": "10004", "improvement": "10002", "feature": "10001"},
  "priorities": ["1", "2", "3", "4", "5"],
  "custom_fields": {"customfield_12345": "your required value"},
  "statuses": {"backlog": "To Do", "in_progress": "In Progress", "done": "Done"},
  "transition_fields": {"Done": {"resolution": {"id": "10000"}}}
}
```

This is the **`jira` section**, not the entire configuration. Omit unused custom
and transition fields. Custom-field values use Jira's native REST shapes, such
as `{"id":"123"}` for a select option. All three issue types must expose labels,
priority and description on their create screens. The board filter must include
the created issues. Status names must exist for each mapped issue type; their
backlog → in-progress → done transitions must be directly reachable. Required
transition fields are checked against each issue's currently available transition.

Find project-specific IDs through Jira administration or these read-only APIs:

- `/rest/api/3/issue/createmeta/APP/issuetypes` — issue types.
- `/rest/api/3/issue/createmeta/APP/issuetypes/<typeId>` — create fields and allowed priorities.
- `/rest/api/3/project/APP/statuses` — workflow status names by issue type.
- `/rest/agile/1.0/board` — boards visible to your account.

Provide credentials in the current process environment, not in files or CLI arguments:

```powershell
$env:JIRA_EMAIL = Read-Host 'Atlassian email'
$jiraSecureToken = Read-Host 'Atlassian API token' -AsSecureString
$env:JIRA_API_TOKEN = [System.Net.NetworkCredential]::new('', $jiraSecureToken).Password

py -3 $tool doctor
py -3 $tool review       # Local JSON recommendations; no Jira calls or source changes
py -3 $tool run --once   # Inspect one complete autonomous cycle first
py -3 $tool status
py -3 $tool run          # Continue indefinitely; Ctrl+C stops and preserves progress
```

Alternatively pass the application directory before the command:
`py -3 $tool --repo 'C:\path\to\application' run --once`.

Optional installation, from this tool's directory: `py -3 -m pip install .`.
The installed command is `self-improve`, with the same arguments.

## Validation and agent workflows

Codex discovers noninteractive local setup, test, lint, build and runtime-smoke
commands from manifests, CI and documentation. Discovery is cached until relevant
project configuration/docs change. Both a test check and runtime check are
required; existing lint/build gates should also be included. If required commands
are missing, `run` reports the discovery reason and stops before publishing or
blocking tickets. You can still inspect recommendations with `review`.

Override discovery with the top-level `commands` setting when needed:

```json
{
  "setup": [
    ["py", "-3", "-m", "venv", ".venv"],
    [".venv/Scripts/python.exe", "-m", "pip", "install", "-r", "requirements.txt"]
  ],
  "checks": [
    {"name": "regression", "kind": "test", "argv": [".venv/Scripts/python.exe", "-m", "unittest", "discover", "-s", "tests"]},
    {"name": "smoke", "kind": "runtime", "argv": [".venv/Scripts/python.exe", "scripts/smoke_test.py"]}
  ],
  "reason": "Application-maintained checks"
}
```

Commands are argv arrays, run from the disposable worktree, without an implicit
shell. Use `powershell.exe -NoProfile -Command ...` explicitly for shell syntax.
Setup must be idempotent and must use test/local services; never production.
Ignore generated dependencies/build artifacts in the application's `.gitignore`.

Each implementation receives the pinned specification, planning, incremental
implementation, testing, Git, documentation and security workflows, plus its
recommended additional skills. A fresh Codex session independently reviews the
committed diff, acceptance criteria, validation evidence, documentation and risks.
The controller executes baseline checks and ticket-specific test/runtime checks,
then reruns them on the committed snapshot. A failing check or rejected review
allows two repair attempts, then blocks the ticket and preserves the worktree.

Noninteractive operation is pre-authorized by the configured run: ordinary
specification and merge approval prompts are replaced with ticket criteria and
the independent review. Unresolved requirements and remaining mandatory human
approval requirements block work. The loop never weakens checks to obtain a pass.
An application without meaningful checks needs those checks established first.

The local target branch must remain clean, on the configured branch and at the
reviewed base commit. Integration is fast-forward only. No push, PR, deployment,
or live database migration is performed. Breaking source changes and migration
code are allowed; migration instructions and rollback guidance are required.

## Recovery and troubleshooting

`status` reads local state without contacting Codex or Jira. Runtime files include:

- `state.db`: checkpoints, recommendations, process evidence and pending Jira operations.
- `run.jsonl`: structured process logs, rotated at 2 MB with three backups.
- `sessions/`: Codex output schemas, structured results and event traces.
- `agent-skills/`: the pinned workflow checkout.

Source worktrees are in a sibling directory named
`.<application>-improver-<repository-id>`, outside the Git metadata directory so
Codex's Git-metadata write protection does not prevent implementation. Failed
work is preserved there; clean successful worktrees and their branches are
removed after integration. Commits remain in the target's history. `status`
includes the selected ticket's worktree path (it may be removed after success).

Only one controller can run per repository (including linked worktrees). A crash
releases its OS lock. Start `run` again to resume before generating new tickets.
A crash after merge does not rerun implementation; Jira completion is reconciled
from the saved commit. Pending updates use durable delivery receipts and
idempotent issue-property/status operations. Evidence is stored in the Jira issue
property `self-improver-result`, accessible at
`/rest/api/3/issue/APP-123/properties/self-improver-result`, and in local status/logs.

Tickets have `self-improver`, `self-improver-app-<app-id>` and fingerprint labels.
Only this application's tool-created tickets are eligible. Actual Jira priority
wins, with oldest creation time and issue key as tie breakers. A ticket carrying
`self-improver-blocked` is excluded. Resolve its blocker and remove that label in
Jira to make it eligible again. Unresolved generated dependencies start blocked.

After an uncertain create response the tool searches Jira for the exact fingerprint.
It **never blindly retries** creation. Jira indexing can take time. If it remains
uncertain, manually check Jira, then use one of:

```powershell
py -3 $tool resolve-create FINGERPRINT --issue APP-123
# Only after confirming that no ticket was created:
py -3 $tool resolve-create FINGERPRINT --not-created
```

Transient failures use exponential backoff (30 seconds up to 30 minutes). Jira's
longer `Retry-After` is honored. Authentication/configuration errors exit with a
diagnostic. Default timeouts are 30 minutes for reviews, 90 minutes for an
implementation and 30 minutes per validation command; all are configurable.
`interval` defaults to 300 seconds. There is no automatic usage/budget cap.

Ctrl+C terminates the running child process tree, saves the existing checkpoint
and leaves unfinished work for inspection. Never delete runtime data while a run
is active. Preserved worktrees/branches and transcripts need periodic operator
archival on long-running installations.

## Boundaries

This is a source-repository improvement tool, not a guarantee that arbitrary
applications improve. Review quality depends on Codex, documentation and tests.
Exact-title/fingerprint duplicates are rejected deterministically; semantic
duplicates are also checked by the review agent and remain a model judgment.
Three tickets is a maximum, not a quota. Backlog size can grow without limit.
Model context contains up to 200 prioritized open-ticket excerpts and 200 recent
historical summaries; deterministic duplicate detection and ticket selection
still inspect the full backlog. Source inventory is capped at 10,000 eligible
paths and embedded documentation at approximately 200 KB; omitted path counts
are disclosed to the agent. Large applications may need focused follow-up work.

Jira credentials are removed from child environments and redacted from process
logs. Repository inventory excludes secret-looking paths, dependencies, generated
files and binaries. Codex receives read-only permissions for reviews and
`--approve-for-me` for implementation; no sandbox-bypass option is used.
Git worktrees are change isolation, **not a security sandbox**: application setup
and tests execute local code under your account. Run only trusted repositories;
use an externally isolated machine/container for untrusted code. No claim is made
that an AI worker cannot read other credentials accessible to its account.

## Development and verification

```powershell
py -3 -m unittest discover -s tests -v
py -3 self_improve.py --help
```

Tests use temporary repositories, fake agents and Jira responses. They do not
create real Jira tickets or consume paid Codex sessions. Live Jira/Codex operation
requires your application configuration and credentials.

Sources: [Codex scripted execution](https://learn.chatgpt.com/docs/developer-commands#codex-exec),
[Jira issue APIs](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/),
[Jira enhanced search](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-search/),
[Jira backlog APIs](https://developer.atlassian.com/cloud/jira/software/rest/api-group-backlog/),
[Atlassian API authentication](https://developer.atlassian.com/cloud/jira/platform/basic-auth-for-rest-apis/).
