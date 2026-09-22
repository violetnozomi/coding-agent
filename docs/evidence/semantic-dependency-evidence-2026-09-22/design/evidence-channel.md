# Evidence ownership

```mermaid
flowchart TD
    RI[Production structural Repo Intelligence] --> RP[RepoRetrievalPolicy]
    RP --> MR[_repo_retrieval_block]
    MR --> MP[ProductionPromptBuilder / Main]
    CT[ChangeTracker changed paths and diff] --> SH[SidecarVerifierHook._evidence]
    RS[RuntimeState verification and retained authority] --> SH
    CT --> DS[Bounded changed_scope direct callers]
    RI --> DS
    DS --> SS[Existing index snapshot: identities, spans, fingerprints]
    SS --> CR[WorkspaceFileAccess current bytes]
    CR --> DE[Dedicated supporting_repository_evidence]
    DE --> SH
    SH --> VC[VerifierContext / rendered packet]
```

Main's production policy already renders direct/impacted callers. Sidecar's
nearby-source reader was limited to blocked-environment review, so normal review
could not receive unchanged caller bodies. The new helper does not reuse the Main
exploration policy or introduce another index/parser. It uses changed_scope then
PersistentCodeIndex.snapshot for definitions/spans/fingerprints. It does not call
symbol_context with unsupported path:symbol syntax.

Only sorted direct callers are considered. Impacted/transitive callers are not
added: the existing graph's impacted_callers currently duplicates direct_callers.
The independent section explicitly denies task, instruction, edit and permission
authority. Existing verifier system/decision prompts, retained reference rendering,
TaskContract, Ledger and completion policy remain unchanged.

The full current hook is replayed, including current compatibility-delta criteria.
Historical-frozen-packet.json preserves the previous CF controlled packet; RED and
GREEN current-hook packets differ only in the new supporting context fields and
rendered section. These offline packets are never sent.
