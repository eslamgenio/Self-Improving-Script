# Application Self-Improver

A Python controller that reviews an application's source with Codex CLI, creates
up to three Jira Cloud backlog tickets per cycle, and implements the
highest-priority eligible ticket in a Git worktree. It verifies
the change and fast-forwards the local target branch.
It repeats five minutes after each cycle until you press Ctrl+C.

Product growth and user experience guide recommendations. Critical security,
data-loss and correctness defects take priority. Features must fit the documented
application purpose. Ambiguous product decisions block the ticket.

## Prerequisites

- Python **3.12 or later** for the CLI. The Windows `setup-scrum.ps1` launcher
  requires Python **3.12** specifically because it calls `py -3.12`.
- Git, with `user.name` and `user.email` configured.
- Codex CLI, authenticated with `codex login`. For the npm installation, Node.js
  must be available. The installed CLI must support `exec --output-schema`,
  `--approve-for-me`, and the read-only sandbox (verified against CLI 0.163.0).
- A local application **Git repository with an initial commit**, a clean working
  tree, and its target branch checked out. Install that application's toolchains.
- Jira **Cloud Software**, a project and board with backlog enabled. The account
  needs Browse Projects, Create Issues, Edit Issues, Transition Issues and
  Schedule Issues, and Add Comments, plus access to the board and issue properties.
- An Atlassian account email and API token. Use the site URL for an unscoped token;
  scoped tokens use `https://api.atlassian.com/ex/jira/<cloudId>` as the base URL.

The Python controller has **no third-party runtime dependencies**. Codex usage
uses the account/model configured in your CLI and is subject to its usage limits.

## Set up the SCRUM application on another Windows machine

Set `jira_url`, `jira_project`, and `jira_board_id` in `setup-scrum.json` to
select the Jira target. Use the `jira` object in that file to set issue type
IDs, priority IDs, workflow statuses, and any required custom or transition
fields.

Clone this tool and the application repository. Keep the application's `main`
branch clean and checked out. Have Python 3.12, Git, a signed-in Codex CLI, and
Docker Desktop running in Linux container mode.

From the tool folder, run:

```powershell
.\setup-scrum.ps1
```

`setup-scrum.ps1` and `self_improve.py` both use the adjacent `setup-scrum.json`
for the application path, Jira target, credentials, and agent-skills commit.
They share runtime state for that Jira target under the application's Git
metadata. To select a different settings file, pass `--launcher-settings`.

Create `setup-scrum.json` beside `setup-scrum.ps1` before running setup if you
want to fill in the values yourself. This example uses the previous SCRUM
settings. Use the application path and Jira mappings for your installation.

```json
{
  "application_path": "C:/Eslam/2- Areas/Projects/Self-Improving App",
  "jira_url": "https://ishussien.atlassian.net",
  "jira_project": "SCRUM",
  "jira_board_id": 1,
  "jira_email": "your-atlassian-email@example.com",
  "jira_api_token": "",
  "jira": {
    "issue_types": {"bug": "10003", "improvement": "10003", "feature": "10004"},
    "priorities": ["1", "2", "3", "4", "5"],
    "statuses": {"backlog": "To Do", "in_progress": "In Progress", "done": "Done"}
  }
}
```

Use an absolute application path with forward slashes or doubled backslashes
in JSON. Enter your Atlassian sign-in email and leave `jira_api_token` blank if
you want to enter it at the masked prompt. The saved token is plain text, and
`.gitignore` excludes the file from Git. Keep the file private.

List priority IDs from highest to lowest. If Jira requires custom fields or
transition fields, add `custom_fields` or `transition_fields` inside `jira`.
After `doctor` passes, the launcher saves the settings and records
`skills_commit` automatically.

After `doctor` passes and settings are saved, press Enter or choose `1` to run
one cycle. Choose `2` to run continuously and press Ctrl+C to stop, or choose `3`
to exit without starting a cycle.

You can also run the workflow directly from the tool folder. In an interactive
terminal, `init`, `doctor`, and `run` read `setup-scrum.json` and prompt for
missing application and Jira settings. The token prompt masks input.
`init` validates and saves the launcher settings locally. `doctor` and `run`
save prompted values after `doctor` passes, before starting any cycle.

```powershell
py -3.12 .\self_improve.py init
py -3.12 .\self_improve.py doctor
py -3.12 .\self_improve.py run --once
py -3.12 .\self_improve.py status
```

Pass `--repo` before the command to override the saved application path.
For unattended runs, fill in `setup-scrum.json` first.

## Use an application repository configuration

Pass `--repo-settings` before commands such as `doctor`, `run`, or `status` to
use the application repository configuration. The installed `self-improve`
command also uses that configuration by default.

Keep this tool outside the application you are improving. From PowerShell:

```powershell
$tool = 'C:\Eslam\2- Areas\Projects\Self-Improving Script\self_improve.py'
Set-Location 'C:\path\to\your-application'

py -3.12 $tool --repo-settings init --app-id my-app `
  --jira-url 'https://YOUR-SITE.atlassian.net' `
  --project APP --board-id 123 `
  --priority-ids '1,2,3,4,5' `
  --bug-type '10004' --improvement-type '10002' --feature-type '10001' `
  --backlog-status 'To Do' --progress-status 'In Progress' --done-status 'Done'
```

**The IDs above are examples, not universal Jira defaults.** Use your instance's
actual IDs, with priorities ordered highest first. Omit a Jira option to enter
its value interactively. Repository-mode `init` clones the official
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

For the application repository configuration, supply Jira credentials through
the current process environment.

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

## Collaborate on Jira tickets

Tickets enter the automation queue when a reviewer moves them from **To Do**
to **Ready for Automation**. An eligible ticket also needs a description and a
mapped priority. The controller excludes tickets labeled `self-improver-blocked`
and tickets assigned to another application. Each new cycle can create up to
three **To Do** tickets before selecting approved work, even when approved work
is already waiting. Each category can contribute one ticket. Jira labels are `UI` for visual
presentation, `Feature` for a new capability, and `UX` for improving an existing
workflow or interaction. UI and UX use the configured improvement issue type.
Feature uses the configured feature issue type.

If any categories are missing, the controller asks Codex once for suggestions
in those categories. It includes accepted suggestions and matching Jira ticket
keys for duplicates when available. It publishes valid suggestions and reports
the ticket key or missing suggestion for each category.

For example, a cycle that finds UI and Feature work reports:

```text
Jira ticket batch: UI: APP-41, Feature: APP-42, UX: no suitable suggestion
```

If a Jira create response is lost partway through a batch, the next run
reconciles the saved fingerprint and resumes the batch.

1. Open a To Do ticket and write the scope and acceptance criteria in the
   description. Review the title and description, then move the ticket to
   **Ready for Automation**. Move it back to **To Do** to withdraw approval.
2. If you change the title or description after approval, the controller uses
   Jira's change history to detect the edit, including edits that were later
   reverted. It returns a ticket with stale approval to **To Do** when it
   detects the change and stops any active work before merging. Review the
   revised scope and move it to **Ready for Automation** again.
3. Keep the workflow transitions from **Ready for Automation** to **In Progress**
   and from both statuses back to **To Do** available to the controller.
   Restrict the transition into **Ready for Automation** to your reviewers in
   Jira if approval should be limited to specific people. If your workflow uses
   another approval status name, set `jira.statuses.approved` in the Jira settings.

For tickets approved under the previous comment-based workflow, review the
current scope and use the status transition. For older work already marked
**In Progress**, first move it to **To Do**.

In a live Jira Cloud test, the controller selected the ticket after approval.
Separate title and description edits each returned it to **To Do**, and a fresh
approval restored eligibility each time. A description edit while the ticket
was **In Progress** also returned it to **To Do** when the approval guard ran.

After a local merge, the controller comments on the ticket with the implementation
summary, check results, and commit ID, then transitions it to Done.

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

If a setup command runs `python -m pip install --no-index -r requirements.txt`
and pip reports a missing distribution, the controller downloads wheels for the
same requirements from pip's configured package index and retries the offline
install with those wheels. The download and retry run once per setup attempt.
If recovery fails, the Jira blocker comment includes the original install
error and the recovery error. Recovery was verified in a fresh Python 3.12
environment with the application's full pinned requirements. The retry
installed `asgiref==3.13.0` and `pip check` passed.

After Jira scope approval, the configured run uses ticket criteria and the
independent review for routine specification and merge checks. Unresolved
requirements and remaining mandatory human approval requirements block work.
The loop never weakens checks to obtain a pass.
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

Generated tickets have `self-improver`, `self-improver-app-<app-id>` and fingerprint
labels. Manual tickets can enter the same approval flow without these labels.
Among eligible Ready for Automation tickets, Jira priority wins, with oldest creation time and
issue key as tie breakers. When automation blocks on a prerequisite or
verification failure, it moves the ticket to **Blocked** and posts the reason
as a Jira comment. The workflow must allow this transition from **Ready for
Automation** and **In Progress**. Set `jira.statuses.blocked` if your workflow
uses a different status name.

To retry after resolving the blocker, remove the `self-improver-blocked` label,
move the ticket to **To Do**, then approve it again. Generated tickets with
unresolved dependencies start in the backlog with the blocker label.

After an uncertain create response the tool searches Jira for the exact fingerprint.
It **never blindly retries** creation. Jira indexing can take time. If it remains
uncertain, manually check Jira, then use one of:

```powershell
py -3 $tool resolve-create FINGERPRINT --issue APP-123
# Only after confirming that no ticket was created:
py -3 $tool resolve-create FINGERPRINT --not-created
```

If Jira comment delivery remains uncertain, check the issue and find the pending
operation ID in `status`. After verifying that the comment is absent, allow a
retry with `py -3 $tool resolve-comment OPERATION_ID --not-posted`.

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
