# NZ-Coder 3–5 minute interview demo

This is a normal product journey, not a demo-only feature. Prepare a small
repository with one failing test and a configured provider before the interview.

1. Open the repository and run `nz-coder`. Point out the workspace basename,
   model, mode, Session title/short ID, and `LOCAL` location in the header.
2. Ask an unknown-location task such as: `Find why the auth retry test fails,
   fix it conservatively, and run the focused tests.`
3. When Repo Intelligence/search activity appears, explain that localization is
   bounded and that the Agent can fall back to grep, Repo Map, and LSP.
4. Approve the scoped edit/command if prompted. Show the edit card and its
   `+N -N` summary.
5. Run `/diff` to inspect the complete change, then show the verification result.
6. Start or inspect a persistent command with the Process tool and use
   `/processes` to show its identity, status, logs, and owner Session.
7. Rename the Session, exit, restart NZ-Coder, and use `/session` or `/resume`
   to demonstrate durable continuation.

Keep the explanation on observable ownership boundaries: one Session, one
workspace, explicit permissions, recoverable diffs, and process cleanup. Do not
claim native Windows validation until the `windows-product-rc.yml` artifact is
green.
