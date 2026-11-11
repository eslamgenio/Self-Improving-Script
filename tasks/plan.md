# Autonomous application improvement implementation

Approved specification: the user's Autonomous Application Improvement Loop plan.

Build order: configuration/state/process control → Git/Codex contracts → Jira adapter
→ checkpointed cycle → CLI/setup → failure testing and documentation.

The controller owns integration and durable Jira effects. Codex owns scoped source
changes and a separate independent review. Target state never changes before
baseline, regression, ticket-specific checks and review pass.

Commands: `py -3 -m unittest discover -s tests -v`; `py -3 self_improve.py --help`.

Runtime data/configuration live in the target repository's Git common directory.
No production deployment, remote pushes or live database migrations.
