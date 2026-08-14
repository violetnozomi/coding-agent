"""Frozen Windows entrypoint for the existing NZ-Coder CLI runtime."""
from __future__ import annotations

from nz_coder.cli import main


raise SystemExit(main())
