"""Tests for the host-neutral product capability snapshot."""
from __future__ import annotations

import pytest

from nz_coder.runtime.execution.product_surfaces import (
    PRODUCT_CAPABILITY_FINGERPRINT,
    ProductSurface,
    capability_snapshot,
)


def test_snapshot_covers_every_product_surface_with_same_contract():
    snapshots = [capability_snapshot(surface) for surface in ProductSurface]

    assert all(snapshot == snapshots[0] for snapshot in snapshots)
    assert [row["name"] for row in snapshots[0]] == sorted(PRODUCT_CAPABILITY_FINGERPRINT)


def test_snapshot_has_exact_unique_names_and_allowed_statuses():
    rows = capability_snapshot(ProductSurface.SDK)
    names = [row["name"] for row in rows]

    assert len(names) == len(set(names))
    assert set(names) == PRODUCT_CAPABILITY_FINGERPRINT
    assert all(set(row) == {"name", "status", "evidence", "parity_note"} for row in rows)
    assert {row["status"] for row in rows} <= {
        "implemented", "partial", "planned", "different-by-design", "unavailable",
    }
    assert all(
        row["evidence"]
        and not row["evidence"].startswith("/")
        and len(row["evidence"]) <= 200
        for row in rows
    )


def test_snapshot_rejects_unknown_surface():
    with pytest.raises(ValueError):
        capability_snapshot("unknown")


def test_snapshot_results_are_mutation_isolated():
    first = capability_snapshot("interactive")
    first[0]["name"] = "changed"
    first[0]["parity_note"] = "changed"
    second = capability_snapshot("interactive")

    assert second[0]["name"] != "changed"
    assert second[0]["parity_note"] != "changed"

