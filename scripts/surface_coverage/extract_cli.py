"""Extract CLI command names from src/nexus/cli/commands/__init__.py.

Parses the `_REGISTER_COMMANDS: dict[str, tuple[str, ...]]` literal via AST so
we don't need to import the package (which has heavy runtime deps).
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
    tree = ast.parse(init_py_path.read_text())
    register_dict: dict[str, tuple[str, ...]] | None = None
    for node in tree.body:
        # Check for both ast.Assign and ast.AnnAssign (annotated assignments)
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_REGISTER_COMMANDS" for t in node.targets
        ):
            register_dict = _literal_dict_of_str_tuples(node.value)
            break
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "_REGISTER_COMMANDS"
        ):
            if node.value is not None:
                register_dict = _literal_dict_of_str_tuples(node.value)
                break
    if register_dict is None:
        raise ValueError(f"_REGISTER_COMMANDS not found in {init_py_path}")

    out: list[RawCliCommand] = []
    commands_dir = init_py_path.parent
    for module_name, command_names in register_dict.items():
        module_file = commands_dir / f"{module_name}.py"
        for cmd in command_names:
            # commands themselves may be single or multi-token; reduce - to space
            invocation = "nexus " + cmd.replace("_", " ")
            out.append(
                RawCliCommand(
                    name=invocation,
                    module_file=module_file,
                    source=f"{module_file}:1",
                )
            )
    return sorted(out, key=lambda r: r.name)


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
