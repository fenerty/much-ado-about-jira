# Product direction

Much ADO About Jira should be the natural place an engineer starts and returns to during the workday: one dependable inventory of related work, and one stream of changes to it. A user should not need to learn hidden ranking rules or test destructive-looking controls to understand the interface.

## Interaction contract

- Updates answers what changed. My work answers what work is related to me. Do not add overlapping primary views.
- My work defaults to visible status groups, with completed items last and recent changes first within each group. Exact source status labels are preserved. Recently updated remains an optional sort.
- Counts use ordinary, literal labels. Open PRs includes authored PRs; PRs needing my review is a distinct subset. Drafts and PRs already voted on are not pending first reviews.
- One work item appears once in My work, even when it has several relationships. Update events can appear separately and say what changed when known.
- Dismissing updates never hides their work item or mutes future updates. Scope is explicit, Undo is available, and retained dismissed entries can be restored.
- Coverage and evidence limitations must be honest. Source-confirmed history, observed snapshot differences, and unavailable details are distinct.

## Intended coverage

Current and previous assignments, authored work, followed/watched items, and work the engineer has commented on or otherwise participated in should remain discoverable as their involvement changes. Updates should continue across handoffs and closure; a current ownership filter must not silently erase an established relationship. The activity retention window is not the same as the age of an established relationship.

## Current gaps to close

- Prior assignment is queried for both issue sources; completed issues are included, but query limits can truncate historical discovery.
- Jira comment discovery is limited to configured projects and lookback windows. It is not an all-time participation index.
- ADO comment discovery starts from retrieved work. Followed subscriptions and arbitrary historic PR participation are not collected.
- Previously discovered work is retained across query omissions with last-known status explicitly labeled. Historical relationship reasons persist. Direct polling of omitted items and complete historical backfill remain gaps; retention alone does not guarantee fresh status or future updates for omitted items.
- ADO field details cover a bounded latest revision; Jira uses local snapshot differences. Neither constitutes a complete cross-system audit trail.
- PR recency currently reflects the most recent fetched discussion activity or creation time; code pushes and all metadata changes are not fully represented.

The next coverage milestone is a durable relationship registry plus bounded backfill and incremental polling. It must preserve known involvement, distinguish closed work from deleted/unavailable work, and report what is and is not collected. Do not claim full historical coverage before validating it.

Read/unread is local per entity and batch actions affect only selected displayed rows. It never dismisses entries or propagates between work and events.

Jira completed detail refresh rotates through 16 discovered closed tickets per refresh; active candidates refresh each time. Search caps still apply. Partial coverage remains explicit during rotation.

Display preferences support 30/50/100/250 rows or all rows. Explicit all-matching selection includes collapsed groups and undisplayed pages. Group order and collapsed state persist per browser. Coverage counts distinguish retained events, distinct event parents, durable work, and bounded discovery; rotating history progress is per process/session pass, with ETA withheld on failed checks.
