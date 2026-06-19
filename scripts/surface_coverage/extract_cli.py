"""Extract CLI command names from src/nexus/cli/commands/__init__.py.

Parses both registration shapes via AST so we don't need to import the package
(which has heavy runtime deps):

- `_REGISTER_COMMANDS: dict[str, tuple[str, ...]]` — modules that expose
  `register_commands(cli)` to add multiple Click commands.
- `_ADD_COMMAND: dict[str, tuple[str, str]]` — modules that expose a single
  Click command/group via `cli.add_command(<attr>)`.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RawCliCommand:
    name: str  # e.g. "nexus fs read"
    module_file: Path
    source: str  # "path:1" (module file; line approximate)


def extract_cli_commands(init_py_path: Path) -> list[RawCliCommand]:
    tree = ast.parse(init_py_path.read_text(encoding="utf-8"))
    register_dict = _find_dict_assignment(tree, "_REGISTER_COMMANDS", _literal_dict_of_str_tuples)
    add_dict = _find_dict_assignment(tree, "_ADD_COMMAND", _literal_dict_of_str_pair, optional=True)

    if register_dict is None:
        raise ValueError(f"_REGISTER_COMMANDS not found in {init_py_path}")

    out: list[RawCliCommand] = []
    commands_dir = init_py_path.parent
    for module_name, command_names in register_dict.items():
        module_file = commands_dir / f"{module_name}.py"
        for cmd in command_names:
            invocation = "nexus " + cmd.replace("_", " ")
            out.append(
                RawCliCommand(
                    name=invocation,
                    module_file=module_file,
                    source=f"{module_file}:1",
                )
            )

    if add_dict:
        for module_name, (command_name, _attr_name) in add_dict.items():
            module_file = commands_dir / f"{module_name}.py"
            invocation = "nexus " + command_name.replace("_", " ")
            out.append(
                RawCliCommand(
                    name=invocation,
                    module_file=module_file,
                    source=f"{module_file}:1",
                )
            )

    # Dedupe (same command name from both dicts is unlikely but possible)
    seen: dict[str, RawCliCommand] = {}
    for entry in out:
        if entry.name not in seen:
            seen[entry.name] = entry
    return sorted(seen.values(), key=lambda r: r.name)


def _find_dict_assignment(
    tree: ast.Module,
    var_name: str,
    parser,
    *,
    optional: bool = False,
):
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == var_name for t in node.targets
        ):
            return parser(node.value)
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == var_name
            and node.value is not None
        ):
            return parser(node.value)
    if optional:
        return None
    raise ValueError(f"{var_name} not found")


def _literal_dict_of_str_tuples(node: ast.AST) -> dict[str, tuple[str, ...]]:
    if not isinstance(node, ast.Dict):
        raise ValueError("expected dict literal")
    out: dict[str, tuple[str, ...]] = {}
    for k_node, v_node in zip(node.keys, node.values, strict=True):
        if not isinstance(k_node, ast.Constant) or not isinstance(k_node.value, str):
            raise ValueError("dict keys must be str literals")
        if not isinstance(v_node, ast.Tuple):
            raise ValueError("dict values must be tuple literals")
        values: list[str] = []
        for elt in v_node.elts:
            if not isinstance(elt, ast.Constant) or not isinstance(elt.value, str):
                raise ValueError("tuple elements must be str literals")
            values.append(elt.value)
        out[k_node.value] = tuple(values)
    return out


def _literal_dict_of_str_pair(node: ast.AST) -> dict[str, tuple[str, str]]:
    """Parse `{str: (str, str)}` (the _ADD_COMMAND shape)."""
    if not isinstance(node, ast.Dict):
        raise ValueError("expected dict literal")
    out: dict[str, tuple[str, str]] = {}
    for k_node, v_node in zip(node.keys, node.values, strict=True):
        if not isinstance(k_node, ast.Constant) or not isinstance(k_node.value, str):
            raise ValueError("dict keys must be str literals")
        if not isinstance(v_node, ast.Tuple) or len(v_node.elts) != 2:
            raise ValueError("dict values must be 2-tuple literals")
        pair = []
        for elt in v_node.elts:
            if not isinstance(elt, ast.Constant) or not isinstance(elt.value, str):
                raise ValueError("tuple elements must be str literals")
            pair.append(elt.value)
        out[k_node.value] = (pair[0], pair[1])
    return out
