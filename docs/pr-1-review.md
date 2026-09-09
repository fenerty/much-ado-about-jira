# PR 1 review assessment

Reviewed source: c673092. Follow-up fixes:

- Active Jira relationships now have separate query budgets from completed history. A capped historical query cannot displace older active assignments, watched work, prior assignments, or authored work.
- Empty optional Jira project lists skip project-scoped comment-discovery queries and explicitly report that discovery as disabled.
- Undo accepts the complete version list produced by a related dismissal, including more than 1,000 events.
- ADO history deadlines preserve completed successful results, cancel unfinished requests, and drain tasks.
- Startup verifies a dedicated application identity endpoint before opening an existing listener. An unrelated listener produces a collision error.
- Assigned to me excludes unverified snapshots, completed items, and PRs, matching the current-assignment summary.

The proposed WIQL syntax correction was not applied. Microsoft's [WIQL reference](https://learn.microsoft.com/en-us/azure/devops/boards/queries/wiql-syntax?view=azure-devops#modifiers-and-special-operators) explicitly documents alternate EVER syntaxes, including `EVER [System.AssignedTo] = ...`. Read-only live verification confirmed the existing and proposed forms both succeed with identical result sets. No private IDs or source contents are included here.

Regression coverage: separate active/history caps, blank project lists, completed-result preservation and cancellation, large related undo, application-versus-unrelated listener checks, collision behavior, and browser verification of snapshot filtering.
