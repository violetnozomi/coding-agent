# Frozen C Semantic-Reviewer Counterfactual

Authorization: current user explicitly allows exactly one physical semantic-verifier
request, no retry, no main/planner/embedding/InfCodeX/other requests.

1. Verify ac47114, clean baseline, immutable production and historical evidence.
2. Reconstruct native ChangeTracker output from committed initial/final bytes and
   historical first-write order; require OLD input byte equality with verifier-2.
3. Use current production hook file-edit projection and production packet builder.
   Freeze all other context fields, including additional criteria. Current full
   hook would also change compatibility delta criteria via the shared splitter;
   this experiment deliberately excludes that second variable as the user requires.
4. Verify unchanged task/spec/transcript/report/contract/ledger/tests/schema/settings;
   offline known-bad workspace must retain 46 passing tests and 11/12 acceptance.
5. Send one direct historical-style httpx POST: retries=0, redirects disabled,
   durable exclusive launch marker and request-count hook. No SDK/Gateway retry.
6. Save sanitized visible response and actual usage. Classify, audit, commit/push,
   stop regardless of verdict. No retry, repair, resample or full C execution.

The initial sandbox read failed before executing; one escalated baseline read
succeeded, a subsequent read was interrupted. Neither made any model request.
