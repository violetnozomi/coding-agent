# SWE-bench Verified Strict Implementation Plan

1. Add benchmark profile definitions and tests for Verified/Lite dataset identity, cardinality, and hint-free prompts.
2. Add a benchmark policy boundary to the Agent runtime: allowlisted local tools, no dynamic/MCP tools, and rejection of undeclared tool calls.
3. Make `run-agent` strict pass@1 by default, add exact-once append/resume state, and separate diagnostic retry artifacts.
4. Extend reproducibility manifests with eligibility, attempt, no-leak, dataset-profile, and artifact-integrity fields.
5. Export sanitized inference-time public trajectories and add an official-layout submission packager with a fail-closed validator.
6. Switch the default model configuration and examples to `deepseek-v4-flash` at the official DeepSeek compatible endpoint.
7. Update EVAL/development learning documentation, then run focused tests, the full suite, and CLI dry-run/help checks.
