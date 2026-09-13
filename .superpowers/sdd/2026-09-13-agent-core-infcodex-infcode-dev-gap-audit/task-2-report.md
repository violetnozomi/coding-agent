# Task 2 report — host-neutral capability snapshot

## Implemented

Added `capability_snapshot(surface)` to `nz_coder.runtime.execution.product_surfaces`.
The function accepts either a `ProductSurface` member or its serialized string value,
validates the surface with the existing enum, and returns a deterministic list sorted by
capability name. It contains exactly the names in `PRODUCT_CAPABILITY_FINGERPRINT`, with
`name`, `status`, `evidence`, and `parity_note` fields on every row.

The manifest is immutable tuple data and each call constructs fresh dictionaries. This
keeps the existing `capability_fingerprint()` behavior unchanged while ensuring callers
cannot mutate later snapshots or module-level contract data. Evidence values are bounded,
repository-relative references. The parity note records that InfCodeX and infcode-dev
host parity still needs external conformance evidence.

## Coverage

Added focused tests covering all four product surfaces, deterministic ordering, exact and
unique capability names, allowed status vocabulary, repository-relative evidence,
unknown-surface rejection, and mutation isolation.

## Verification

```text
pytest -q tests/runtime/test_product_capability_snapshot.py tests/runtime/test_product_surface_parity.py
8 passed in 0.99s
python -m compileall -q nz_coder/runtime/execution/product_surfaces.py
```

## Concerns

The snapshot is a host-neutral declaration. It does not claim that InfCodeX or infcode-dev
implementations are behaviorally equivalent; that remains an integration/conformance task.
Two capabilities (`media_preflight` and `web_search`) are marked `partial` because their
runtime support depends on optional/provider-specific paths.
