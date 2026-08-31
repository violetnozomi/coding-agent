"""Optional tool-pack loader for language- or task-specific tools."""
from __future__ import annotations

from nz_coder.tools import list_optional_packs, load_optional_pack, register, register_optional_pack

register_optional_pack(
    'python_ast',
    module='nz_coder.tools.python_ast',
    tool_names=['python_symbol_check', 'python_structural_edit'],
    tool_effects={
        'python_symbol_check': 'read',
        'python_structural_edit': 'write',
    },
    description='Python-only AST symbol check and structural edit tools.',
)
register_optional_pack(
    'lsp',
    module='nz_coder.tools.lsp',
    tool_names=['lsp'],
    tool_effects={'lsp': 'read'},
    description=(
        'Semantic navigation and diagnostics through an installed language '
        'server. No server is downloaded automatically.'
    ),
)


def _format_pack(pack: dict) -> str:
    status = 'loaded' if pack.get('loaded') else 'unloaded'
    tools = ', '.join(pack.get('tool_names', [])) or '(none)'
    return f"- {pack['name']} [{status}]: {pack['description']} Tools: {tools}"


def load_optional_tools(packs: list[str] | None = None) -> str:
    """Load optional tool packs and expose their schemas on the next model turn."""
    if not packs:
        available = list_optional_packs()
        if not available:
            return 'No optional tool packs are registered.'
        lines = [
            'Optional tool packs:',
            *[_format_pack(pack) for pack in available],
            '',
            'Call load_optional_tools with a packs array to load one or more packs. Example: {"packs": ["python_ast"]}',
        ]
        return "\n".join(lines)

    requested = []
    for item in packs:
        name = str(item or '').strip()
        if not name:
            continue
        if name not in requested:
            requested.append(name)
    if not requested:
        return 'Error: packs must contain at least one non-empty pack name'

    loaded: list[dict] = []
    for name in requested:
        loaded.append(load_optional_pack(name))

    lines = ['Loaded optional tool packs:']
    for pack in loaded:
        lines.append(_format_pack(pack))
    lines.append('The newly loaded tools will be available on the next model turn.')
    return "\n".join(lines)


register(
    name='load_optional_tools',
    description=(
        'Load optional tool packs that are not exposed by default. Use this for '
        'language-specific tools such as the python_ast pack.'
    ),
    parameters={
        'type': 'object',
        'properties': {
            'packs': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': 'Optional pack names to load. Omit to list available packs.',
            },
        },
    },
    handler=load_optional_tools,
    plan_mode_allowed=True,
)
