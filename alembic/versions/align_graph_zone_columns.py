"""Align graph storage tenant columns with the zone-scoped ORM models.

Revision ID: align_graph_zone_columns
Revises: add_chunk_heading_prefix
Create Date: 2026-06-28
"""

import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal, Union

import sqlalchemy as sa
from sqlalchemy.engine.interfaces import ReflectedIndex, ReflectedUniqueConstraint

from alembic import op

revision: str = "align_graph_zone_columns"
down_revision: Union[str, Sequence[str], None] = "add_chunk_heading_prefix"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_GRAPH_TABLES = ("entities", "relationships")
_INDEX_RENAMES = (
    ("entities", "idx_entities_tenant", "idx_entities_zone"),
    ("entities", "idx_entities_tenant_type", "idx_entities_zone_type"),
    ("relationships", "idx_relationships_tenant", "idx_relationships_zone"),
)
_UNIQUE_RENAMES = (("entities", "uq_entity_tenant_name", "uq_entity_zone_name"),)


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _columns(table_name: str) -> dict[str, Any]:
    inspector = _inspector()
    if table_name not in inspector.get_table_names():
        return {}
    return {column["name"]: column for column in inspector.get_columns(table_name)}


def _index_names(table_name: str) -> set[str]:
    if not _columns(table_name):
        return set()
    return {
        name
        for index in _inspector().get_indexes(table_name)
        if isinstance((name := index.get("name")), str)
    }


def _unique_constraint_names(table_name: str) -> set[str]:
    if not _columns(table_name):
        return set()
    return {
        name
        for constraint in _inspector().get_unique_constraints(table_name)
        if isinstance((name := constraint.get("name")), str)
    }


def _is_string_64(column: Any) -> bool:
    column_type = column["type"]
    return isinstance(column_type, sa.String) and column_type.length == 64


def _quoted_identifier(identifier: str) -> str:
    return op.get_bind().dialect.identifier_preparer.quote(identifier)


def _reflected_name(reflected_object: Any) -> str:
    name = reflected_object.get("name")
    if not isinstance(name, str):
        raise RuntimeError("reflected schema object has no usable name")
    return name


def _replace_sql_identifier(value: str, old_column: str, new_column: str) -> str:
    """Replace an exact SQL identifier without rewriting literals or comments."""
    chunks: list[str] = []
    index = 0
    while index < len(value):
        if value.startswith("--", index):
            end = value.find("\n", index)
            if end == -1:
                chunks.append(value[index:])
                break
            chunks.append(value[index:end])
            index = end
            continue

        if value.startswith("/*", index):
            end = index + 2
            depth = 1
            while end < len(value) and depth:
                if value.startswith("/*", end):
                    depth += 1
                    end += 2
                elif value.startswith("*/", end):
                    depth -= 1
                    end += 2
                else:
                    end += 1
            chunks.append(value[index:end])
            index = end
            continue

        character = value[index]
        if character == "'":
            prefix_index = index - 1
            escape_string = (
                prefix_index >= 0
                and value[prefix_index] in {"E", "e"}
                and (
                    prefix_index == 0
                    or not (
                        value[prefix_index - 1] in {"_", "$"} or value[prefix_index - 1].isalnum()
                    )
                )
            )
            end = index + 1
            while end < len(value):
                if escape_string and value[end] == "\\":
                    end = min(end + 2, len(value))
                    continue
                if value[end] != "'":
                    end += 1
                    continue
                if end + 1 < len(value) and value[end + 1] == "'":
                    end += 2
                    continue
                end += 1
                break
            chunks.append(value[index:end])
            index = end
            continue

        if character == '"':
            end = index + 1
            quoted_identifier_chars: list[str] = []
            while end < len(value):
                if value[end] != '"':
                    quoted_identifier_chars.append(value[end])
                    end += 1
                    continue
                if end + 1 < len(value) and value[end + 1] == '"':
                    quoted_identifier_chars.append('"')
                    end += 2
                    continue
                end += 1
                break
            if end <= len(value) and "".join(quoted_identifier_chars) == old_column:
                chunks.append(f'"{new_column.replace(chr(34), chr(34) * 2)}"')
            else:
                chunks.append(value[index:end])
            index = end
            continue

        if character == "$":
            delimiter_match = re.match(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$", value[index:])
            if delimiter_match:
                delimiter = delimiter_match.group(0)
                end = value.find(delimiter, index + len(delimiter))
                if end != -1:
                    end += len(delimiter)
                    chunks.append(value[index:end])
                    index = end
                    continue

        if character == "_" or character.isalpha():
            end = index + 1
            while end < len(value) and (value[end] in {"_", "$"} or value[end].isalnum()):
                end += 1
            identifier = value[index:end]
            chunks.append(new_column if identifier == old_column else identifier)
            index = end
            continue

        chunks.append(character)
        index += 1

    return "".join(chunks)


def _transform_reflected_value(value: Any, old_column: str, new_column: str) -> Any:
    if isinstance(value, sa.sql.elements.TextClause):
        return sa.text(_replace_sql_identifier(value.text, old_column, new_column))
    if isinstance(value, str):
        return _replace_sql_identifier(value, old_column, new_column)
    if isinstance(value, Mapping):
        return {
            _transform_reflected_value(key, old_column, new_column): _transform_reflected_value(
                item,
                old_column,
                new_column,
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_transform_reflected_value(item, old_column, new_column) for item in value]
    if isinstance(value, tuple):
        return tuple(_transform_reflected_value(item, old_column, new_column) for item in value)
    if isinstance(value, set):
        return {_transform_reflected_value(item, old_column, new_column) for item in value}
    if isinstance(value, frozenset):
        return frozenset(_transform_reflected_value(item, old_column, new_column) for item in value)
    return value


def _reflected_value_references_identifier(value: Any, identifier: str) -> bool:
    replacement = f"__nexus_migration_replacement_{identifier}__"
    if isinstance(value, sa.sql.elements.TextClause):
        return _replace_sql_identifier(value.text, identifier, replacement) != value.text
    if isinstance(value, str):
        return _replace_sql_identifier(value, identifier, replacement) != value
    if isinstance(value, Mapping):
        return any(
            _reflected_value_references_identifier(key, identifier)
            or _reflected_value_references_identifier(item, identifier)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_reflected_value_references_identifier(item, identifier) for item in value)
    return False


def _reflected_object_depends_on_column(reflected_object: Any, column_name: str) -> bool:
    return any(
        key in reflected_object
        and _reflected_value_references_identifier(reflected_object[key], column_name)
        for key in (
            "column_names",
            "dialect_options",
            "include_columns",
            "expressions",
            "column_sorting",
        )
    )


def _transformed_columns(
    reflected_object: Any,
    old_column: str,
    new_column: str,
) -> tuple[str | None, ...]:
    return tuple(
        new_column if column_name == old_column else column_name
        for column_name in (reflected_object.get("column_names") or ())
    )


def _transformed_dialect_options(
    reflected_object: Any,
    old_column: str,
    new_column: str,
) -> dict[str, Any]:
    options = reflected_object.get("dialect_options") or {}
    if not isinstance(options, Mapping):
        return {}
    return {
        str(key): _transform_reflected_value(value, old_column, new_column)
        for key, value in options.items()
    }


def _transformed_index_options(
    reflected_index: Any,
    old_column: str,
    new_column: str,
    dialect_name: str,
) -> dict[str, Any]:
    options = _transformed_dialect_options(reflected_index, old_column, new_column)
    if (
        dialect_name == "postgresql"
        and "postgresql_include" not in options
        and reflected_index.get("include_columns")
    ):
        options["postgresql_include"] = _transform_reflected_value(
            reflected_index["include_columns"],
            old_column,
            new_column,
        )
    return options


def _transformed_index_elements(
    reflected_index: Any,
    old_column: str,
    new_column: str,
) -> list[Any]:
    expressions = reflected_index.get("expressions") or ()
    if isinstance(expressions, str) or not isinstance(expressions, Sequence):
        expressions = ()
    sorting = _transform_reflected_value(
        reflected_index.get("column_sorting") or {},
        old_column,
        new_column,
    )
    if not isinstance(sorting, Mapping):
        sorting = {}

    elements: list[Any] = []
    for position, column_name in enumerate(reflected_index.get("column_names") or ()):
        is_expression = column_name is None
        if is_expression:
            if position >= len(expressions) or not isinstance(expressions[position], str):
                raise RuntimeError("reflected expression index has no usable expression")
            element = _replace_sql_identifier(expressions[position], old_column, new_column)
        else:
            element = _replace_sql_identifier(column_name, old_column, new_column)

        sorting_flags = sorting.get(element, ())
        if sorting_flags:
            sorting_sql = {
                "asc": "ASC",
                "desc": "DESC",
                "nulls_first": "NULLS FIRST",
                "nulls_last": "NULLS LAST",
            }
            try:
                suffix = " ".join(sorting_sql[flag] for flag in sorting_flags)
            except KeyError as error:
                raise RuntimeError(
                    f"unsupported reflected index sorting flag: {error.args[0]}"
                ) from error
            element = f"{element} {suffix}"
            is_expression = True

        elements.append(sa.text(element) if is_expression else element)
    return elements


def _signature_value(value: Any) -> str:
    if isinstance(value, sa.sql.elements.TextClause):
        return f"sql:{value.text}"
    if isinstance(value, Mapping):
        items = sorted(
            (_signature_value(key), _signature_value(item)) for key, item in value.items()
        )
        return f"mapping:{items!r}"
    if isinstance(value, (list, tuple)):
        return f"sequence:{tuple(_signature_value(item) for item in value)!r}"
    if isinstance(value, (set, frozenset)):
        return f"set:{tuple(sorted(_signature_value(item) for item in value))!r}"
    return f"{type(value).__qualname__}:{value!r}"


def _logical_signature(
    reflected_object: Any,
    old_column: str,
    new_column: str,
    *,
    unique: bool,
) -> tuple[tuple[str | None, ...], bool, tuple[tuple[str, str], ...]]:
    details: dict[str, Any] = {}
    for key in ("dialect_options", "expressions", "include_columns", "column_sorting"):
        if key not in reflected_object:
            continue
        value = reflected_object[key]
        if isinstance(value, (Mapping, list, tuple, set, frozenset)) and not value:
            continue
        details[key] = _transform_reflected_value(value, old_column, new_column)
    return (
        _transformed_columns(reflected_object, old_column, new_column),
        unique,
        tuple(sorted((key, _signature_value(value)) for key, value in details.items())),
    )


def _legacy_dependencies(
    table_name: str,
    old_column: str,
) -> tuple[list[ReflectedIndex], list[ReflectedUniqueConstraint]]:
    """Return real indexes and unique constraints that depend on the old column."""
    inspector = _inspector()
    indexes_by_name: dict[str, ReflectedIndex] = {}
    for index in inspector.get_indexes(table_name):
        name = index.get("name")
        if (
            not isinstance(name, str)
            or name in indexes_by_name
            or not _reflected_object_depends_on_column(index, old_column)
            # PostgreSQL exposes a unique constraint's backing index here too.
            # The constraint owns that index, so it must never be treated as a
            # separately droppable/recreatable index.
            or index.get("duplicates_constraint")
        ):
            continue
        indexes_by_name[name] = index

    uniques_by_name: dict[str, ReflectedUniqueConstraint] = {}
    for constraint in inspector.get_unique_constraints(table_name):
        name = constraint.get("name")
        if (
            not isinstance(name, str)
            or name in uniques_by_name
            or not _reflected_object_depends_on_column(constraint, old_column)
        ):
            continue
        uniques_by_name[name] = constraint

    return list(indexes_by_name.values()), list(uniques_by_name.values())


def _renamed_object(
    table_name: str,
    object_name: str,
    renames: tuple[tuple[str, str, str], ...],
    direction: Literal["upgrade", "downgrade"],
) -> str:
    for mapped_table, tenant_name, zone_name in renames:
        if mapped_table != table_name:
            continue
        old_name, new_name = (
            (tenant_name, zone_name) if direction == "upgrade" else (zone_name, tenant_name)
        )
        if object_name == old_name:
            return new_name
    return object_name


def _merge_and_remove_old_column(
    table_name: str,
    old_column: str,
    new_column: str,
    direction: Literal["upgrade", "downgrade"],
) -> None:
    """Merge a partially aligned table without rebuilding it on SQLite."""
    legacy_indexes, legacy_uniques = _legacy_dependencies(table_name, old_column)
    dialect_name = op.get_bind().dialect.name

    if dialect_name == "sqlite" and legacy_uniques:
        raise RuntimeError(
            f"cannot safely remove {old_column} from {table_name} on SQLite because "
            "a unique constraint depends on the legacy column"
        )

    quoted_table = _quoted_identifier(table_name)
    quoted_old = _quoted_identifier(old_column)
    quoted_new = _quoted_identifier(new_column)
    op.execute(
        sa.text(
            f"UPDATE {quoted_table} SET {quoted_new} = {quoted_old} "
            f"WHERE ({quoted_new} IS NULL OR {quoted_new} = '') "
            f"AND {quoted_old} IS NOT NULL"
        )
    )

    dropped_index_names = {index["name"] for index in legacy_indexes}
    dropped_unique_names = {constraint["name"] for constraint in legacy_uniques}
    surviving_index_names = _index_names(table_name) - dropped_index_names
    surviving_unique_names = _unique_constraint_names(table_name) - dropped_unique_names
    inspector = _inspector()
    surviving_index_signatures = {
        _logical_signature(
            index,
            old_column,
            new_column,
            unique=bool(index.get("unique", False)),
        )
        for index in inspector.get_indexes(table_name)
        if index.get("name") not in dropped_index_names and not index.get("duplicates_constraint")
    }
    surviving_unique_signatures = {
        _logical_signature(
            constraint,
            old_column,
            new_column,
            unique=True,
        )
        for constraint in inspector.get_unique_constraints(table_name)
        if constraint.get("name") not in dropped_unique_names
    }
    recreated_index_names: set[str] = set()
    recreated_unique_names: set[str] = set()
    new_info = _columns(table_name)[new_column]

    for index in legacy_indexes:
        op.drop_index(_reflected_name(index), table_name=table_name)
    for constraint in legacy_uniques:
        op.drop_constraint(_reflected_name(constraint), table_name, type_="unique")

    if dialect_name == "sqlite":
        op.execute(sa.text(f"ALTER TABLE {quoted_table} DROP COLUMN {quoted_old}"))
    else:
        op.drop_column(table_name, old_column)
        if not _is_string_64(new_info):
            op.alter_column(
                table_name,
                new_column,
                existing_type=new_info["type"],
                existing_nullable=new_info["nullable"],
                type_=sa.String(length=64),
            )

    for index in legacy_indexes:
        index_name = _reflected_name(index)
        target_name = _renamed_object(
            table_name,
            index_name,
            _INDEX_RENAMES,
            direction,
        )
        signature = _logical_signature(
            index,
            old_column,
            new_column,
            unique=bool(index.get("unique", False)),
        )
        if (
            target_name in surviving_index_names
            or target_name in recreated_index_names
            or signature in surviving_index_signatures
        ):
            continue
        op.create_index(
            target_name,
            table_name,
            _transformed_index_elements(index, old_column, new_column),
            unique=bool(index.get("unique", False)),
            **_transformed_index_options(index, old_column, new_column, dialect_name),
        )
        recreated_index_names.add(target_name)
        surviving_index_signatures.add(signature)

    for constraint in legacy_uniques:
        constraint_name = _reflected_name(constraint)
        target_name = _renamed_object(
            table_name,
            constraint_name,
            _UNIQUE_RENAMES,
            direction,
        )
        signature = _logical_signature(
            constraint,
            old_column,
            new_column,
            unique=True,
        )
        if (
            target_name in surviving_unique_names
            or target_name in recreated_unique_names
            or signature in surviving_unique_signatures
        ):
            continue
        op.create_unique_constraint(
            target_name,
            table_name,
            [
                new_column if column_name == old_column else column_name
                for column_name in constraint["column_names"]
                if column_name is not None
            ],
            **_transformed_dialect_options(constraint, old_column, new_column),
        )
        recreated_unique_names.add(target_name)
        surviving_unique_signatures.add(signature)


def _assert_no_conflicting_values(table_name: str, old_column: str, new_column: str) -> None:
    columns = _columns(table_name)
    if old_column not in columns or new_column not in columns:
        return

    quoted_table = _quoted_identifier(table_name)
    quoted_old = _quoted_identifier(old_column)
    quoted_new = _quoted_identifier(new_column)
    conflict_count = (
        op.get_bind()
        .execute(
            sa.text(
                f"SELECT COUNT(*) FROM {quoted_table} "
                f"WHERE {quoted_new} IS NOT NULL AND {quoted_new} <> '' "
                f"AND {quoted_old} IS NOT NULL AND {quoted_new} <> {quoted_old}"
            )
        )
        .scalar_one()
    )
    if conflict_count:
        raise RuntimeError(
            f"Conflicting {old_column} and {new_column} values in {table_name}; "
            "refusing to modify graph data"
        )


def _assert_sqlite_mixed_schema_is_safe(
    table_name: str,
    old_column: str,
    new_column: str,
) -> None:
    if op.get_bind().dialect.name != "sqlite":
        return
    columns = _columns(table_name)
    if old_column not in columns or new_column not in columns:
        return
    _, legacy_uniques = _legacy_dependencies(table_name, old_column)
    if legacy_uniques:
        raise RuntimeError(
            f"cannot safely remove {old_column} from {table_name} on SQLite because "
            "a unique constraint depends on the legacy column"
        )


def _preflight_alignment(old_column: str, new_column: str) -> None:
    for table_name in _GRAPH_TABLES:
        _assert_no_conflicting_values(table_name, old_column, new_column)
        _assert_sqlite_mixed_schema_is_safe(table_name, old_column, new_column)


def _align_column(
    table_name: str,
    old_column: str,
    new_column: str,
    direction: Literal["upgrade", "downgrade"],
) -> None:
    columns = _columns(table_name)
    if not columns:
        return

    has_old = old_column in columns
    has_new = new_column in columns
    if has_old and has_new:
        _merge_and_remove_old_column(table_name, old_column, new_column, direction)
        return
    if has_old:
        old_info = columns[old_column]
        if op.get_bind().dialect.name == "sqlite":
            quoted_table = _quoted_identifier(table_name)
            quoted_old = _quoted_identifier(old_column)
            quoted_new = _quoted_identifier(new_column)
            op.execute(
                sa.text(f"ALTER TABLE {quoted_table} RENAME COLUMN {quoted_old} TO {quoted_new}")
            )
        else:
            op.alter_column(
                table_name,
                old_column,
                new_column_name=new_column,
                existing_type=old_info["type"],
                existing_nullable=old_info["nullable"],
                type_=sa.String(length=64),
            )
        return
    if (
        has_new
        and op.get_bind().dialect.name != "sqlite"
        and not _is_string_64(columns[new_column])
    ):
        new_info = columns[new_column]
        op.alter_column(
            table_name,
            new_column,
            existing_type=new_info["type"],
            existing_nullable=new_info["nullable"],
            type_=sa.String(length=64),
        )


def _rename_postgresql_schema_objects(
    direction: Literal["upgrade", "downgrade"],
) -> None:
    """Rename legacy PostgreSQL object names after the column rename."""
    if op.get_bind().dialect.name != "postgresql":
        return

    for table_name, tenant_name, zone_name in _INDEX_RENAMES:
        old_name, new_name = (
            (tenant_name, zone_name) if direction == "upgrade" else (zone_name, tenant_name)
        )
        names = _index_names(table_name)
        if old_name in names and new_name not in names:
            op.execute(sa.text(f"ALTER INDEX {old_name} RENAME TO {new_name}"))

    for table_name, tenant_name, zone_name in _UNIQUE_RENAMES:
        old_name, new_name = (
            (tenant_name, zone_name) if direction == "upgrade" else (zone_name, tenant_name)
        )
        names = _unique_constraint_names(table_name)
        if old_name in names and new_name not in names:
            op.execute(
                sa.text(f"ALTER TABLE {table_name} RENAME CONSTRAINT {old_name} TO {new_name}")
            )


def upgrade() -> None:
    _preflight_alignment("tenant_id", "zone_id")
    for table_name in _GRAPH_TABLES:
        _align_column(table_name, "tenant_id", "zone_id", "upgrade")
    _rename_postgresql_schema_objects("upgrade")


def downgrade() -> None:
    _preflight_alignment("zone_id", "tenant_id")
    for table_name in _GRAPH_TABLES:
        _align_column(table_name, "zone_id", "tenant_id", "downgrade")
    _rename_postgresql_schema_objects("downgrade")
