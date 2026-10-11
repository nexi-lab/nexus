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
    source: str  # decorator line, or module line 1 when no declaration is available


def extract_cli_commands(init_py_path: Path) -> list[RawCliCommand]:
    tree = ast.parse(init_py_path.read_text(encoding="utf-8"))
    register_dict = _find_dict_assignment(tree, "_REGISTER_COMMANDS", _literal_dict_of_str_tuples)
    add_dict = _find_dict_assignment(tree, "_ADD_COMMAND", _literal_dict_of_str_pair, optional=True)

    if register_dict is None:
        raise ValueError(f"_REGISTER_COMMANDS not found in {init_py_path}")

    out: list[RawCliCommand] = []
    root_attributes: dict[Path, dict[str, str]] = {}
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
        for module_name, (command_name, attr_name) in add_dict.items():
            module_file = commands_dir / f"{module_name}.py"
            invocation = "nexus " + command_name.replace("_", " ")
            root_attributes.setdefault(module_file, {})[attr_name] = invocation
            out.append(
                RawCliCommand(
                    name=invocation,
                    module_file=module_file,
                    source=f"{module_file}:1",
                )
            )

    # Click registration replaces earlier commands with the same public name.
    seen = {entry.name: entry for entry in out}
    registered = list(seen.values())
    for module_file in sorted({entry.module_file for entry in registered}):
        attributes = {
            function: name
            for function, name in root_attributes.get(module_file, {}).items()
            if any(entry.name == name and entry.module_file == module_file for entry in registered)
        }
        for entry in _nested_commands(module_file, registered, attributes):
            seen[entry.name] = entry
    return sorted(seen.values(), key=lambda r: r.name)


def _nested_commands(
    module_file: Path,
    registered: list[RawCliCommand],
    root_attributes: dict[str, str],
) -> list[RawCliCommand]:
    """Follow Click decorators from registered roots without importing the module."""
    if not module_file.exists():
        return []
    module_tree = ast.parse(module_file.read_text(encoding="utf-8"))
    commands: dict[str, tuple[str | None, str, int]] = {}
    for node in module_tree.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for deco in node.decorator_list:
            if not (
                isinstance(deco, ast.Call)
                and isinstance(deco.func, ast.Attribute)
                and deco.func.attr in {"command", "group"}
                and isinstance(deco.func.value, ast.Name)
            ):
                continue
            name = node.name.replace("_", "-")
            for keyword in deco.keywords:
                if (
                    keyword.arg == "name"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ):
                    name = keyword.value.value
            if (
                deco.args
                and isinstance(deco.args[0], ast.Constant)
                and isinstance(deco.args[0].value, str)
            ):
                name = deco.args[0].value
            parent = deco.func.value.id
            commands[node.name] = (None if parent == "click" else parent, name, deco.lineno)
    roots = {
        function: entry.name
        for entry in registered
        if entry.module_file == module_file
        for function, (parent, name, _) in commands.items()
        if parent is None and name == entry.name.removeprefix("nexus ")
    }
    roots.update(
        {function: name for function, name in root_attributes.items() if function in commands}
    )

    def invocation(function: str, visiting: frozenset = frozenset()) -> str | None:
        if function in visiting:
            raise ValueError(f"Click command cycle at {module_file}:{function}")
        if function in roots:
            return roots[function]
        parent, name, _ = commands[function]
        if parent is None or parent not in commands:
            return None
        base = invocation(parent, visiting | {function})
        return f"{base} {name}" if base else None

    out: list[RawCliCommand] = []
    for function, (_, _, line) in commands.items():
        name = invocation(function)
        if name is not None:
            out.append(RawCliCommand(name, module_file, f"{module_file}:{line}"))
    return out


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
