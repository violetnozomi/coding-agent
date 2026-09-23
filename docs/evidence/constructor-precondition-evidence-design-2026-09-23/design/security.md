# Security and authority

The selected source is read from the current workspace through existing safe
workspace access rules, with path confinement, symlink rejection, byte bounds,
and evaluator/oracle exclusion. Source text is never executed. It is rendered
under a repository-supporting-evidence label and cannot grant task authority,
permissions, or instructions. Unready, stale, unresolved, malformed, or
out-of-budget graph results are omitted.

