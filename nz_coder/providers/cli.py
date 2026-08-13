"""Command-line model discovery and workspace selection."""
from __future__ import annotations

import argparse
from dataclasses import replace

from nz_coder.providers.models import (
    DiscoveredModel,
    active_model_selection,
    cache_status,
    cached_models,
    clear_model_selection,
    configured_catalog_models,
    discover_models,
    model_details,
    save_model_selection,
)
from nz_coder.providers.registry import (
    registry_models,
    registry_status,
    sync_model_registry,
)


def build_parser() -> argparse.ArgumentParser:
    """Build the ``nz-coder models`` parser."""
    parser = argparse.ArgumentParser(prog="nz-coder models")
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="List cached and locally configured models")
    listing.add_argument("--provider")
    listing.add_argument("--details", action="store_true")
    refresh = commands.add_parser("refresh", help="Explicitly query one provider model endpoint")
    refresh.add_argument("--provider")
    sync = commands.add_parser("sync", help="Refresh the models.dev capability registry")
    sync.add_argument("--url")
    sync.add_argument("--force", action="store_true")
    commands.add_parser("registry-status", help="Show the offline registry cache status")
    select = commands.add_parser("select", help="Persist one workspace model choice")
    select.add_argument("model")
    select.add_argument("--provider")
    select.add_argument("--variant")
    commands.add_parser("current", help="Show the effective workspace model")
    commands.add_parser("reset", help="Remove the workspace selection")
    return parser


def models_main(argv: list[str] | None = None) -> int:
    """Run the model management CLI without starting an Agent."""
    args = build_parser().parse_args(argv)
    try:
        if args.command == "refresh":
            models = discover_models(args.provider)
            print(f"Cached {len(models)} model(s) for {models[0].provider}.")
            _print_models(models, details=False)
            return 0
        if args.command == "sync":
            result = sync_model_registry(args.url, force=args.force)
            action = "Refreshed" if result.refreshed else "Using fresh cached"
            print(
                f"{action} model registry: {result.provider_count} provider(s), "
                f"{result.model_count} model(s)."
            )
            return 0
        if args.command == "registry-status":
            status = registry_status()
            if not status.get("available"):
                print("No model registry cache. Run 'nz-coder models sync'.")
                return 0
            print(
                f"source={status['source']} fetched_at={status['fetched_at']} "
                f"providers={status['provider_count']} models={status['model_count']} "
                f"fresh={str(status['fresh']).lower()}"
            )
            return 0
        if args.command == "list":
            models = _merged_models(args.provider)
            if not models:
                print("No cached or locally configured models. Run 'nz-coder models refresh'.")
                return 0
            _print_models(models, details=args.details)
            for provider, fetched_at in sorted(cache_status().items()):
                if not args.provider or args.provider.strip().lower() == provider:
                    print(f"cache {provider}: {fetched_at}")
            return 0
        if args.command == "select":
            provider, model_id = _selection_parts(args.model, args.provider)
            selection = save_model_selection(provider, model_id, variant=args.variant)
            suffix = f" variant={selection.variant}" if selection.variant else ""
            print(f"Selected {selection.provider}/{selection.model_id}{suffix} for this workspace.")
            return 0
        if args.command == "current":
            selection = active_model_selection()
            suffix = f" variant={selection.variant}" if selection.variant else ""
            print(f"{selection.provider}/{selection.model_id}{suffix} ({selection.source})")
            return 0
        if args.command == "reset":
            removed = clear_model_selection()
            print("Workspace model selection removed." if removed else "No workspace model selection exists.")
            return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Error: {exc}")
        return 1
    return 2


def _selection_parts(value: str, provider: str | None) -> tuple[str, str]:
    candidate = value.strip()
    if provider:
        if not candidate:
            raise ValueError("Model id must not be empty")
        return provider.strip().lower(), candidate
    if "/" not in candidate:
        raise ValueError("Use PROVIDER/MODEL or pass --provider")
    selected_provider, model_id = candidate.split("/", 1)
    if not selected_provider or not model_id:
        raise ValueError("Use PROVIDER/MODEL or pass --provider")
    return selected_provider.lower(), model_id


def _merged_models(provider: str | None) -> list[DiscoveredModel]:
    wanted = provider.strip().lower() if provider else None
    merged = {
        (item.provider, item.model_id): DiscoveredModel(
            item.provider,
            item.model_id,
            item.name,
        )
        for item in registry_models()
        if not wanted or item.provider == wanted
    }
    merged.update(
        {(item.provider, item.model_id): item for item in cached_models(wanted)}
    )
    for item in configured_catalog_models():
        if wanted and item.provider != wanted:
            continue
        merged[(item.provider, item.model_id)] = replace(
            item,
            display_name=item.display_name or "local catalog",
        )
    return sorted(merged.values(), key=lambda item: (item.provider, item.model_id.lower()))


def _print_models(models: list[DiscoveredModel], *, details: bool) -> None:
    active = active_model_selection()
    for item in models:
        marker = "*" if (item.provider, item.model_id) == (active.provider, active.model_id) else " "
        line = f"{marker} {item.provider}/{item.model_id}"
        label = item.display_name or item.owned_by
        if label:
            line += f"  {label}"
        if details:
            capability = model_details(item)
            line += (
                f"  family={capability.family} context={capability.context_tokens}"
                f" output={capability.output_tokens} tools={str(capability.supports_tools).lower()}"
            )
        print(line)


if __name__ == "__main__":
    raise SystemExit(models_main())
