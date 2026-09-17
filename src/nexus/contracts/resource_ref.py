"""Provisional Nexus-owned adapter for the Product ``ResourceRef`` wire object.

This module validates the Nexus-owned envelope and extension policy with JSON
Schema Draft 2020-12.  It consumes exact-pinned, byte-verified ``ZoneId``
and ``ZonePath`` projections from nexus-vfs without restating their rules.  The
adjacent source lock records the immutable owner revision and complete offline
reference closure.
"""

from __future__ import annotations

import copy
import json
import math
import types
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from functools import cache
from importlib import resources
from typing import Any, TypeAlias, cast

from jsonschema import Draft202012Validator, validators
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from nexus.contracts.exceptions import ValidationError

RESOURCE_REF_API_VERSION = "common.sudo.dev/v1"
RESOURCE_REF_KIND = "ResourceRef"
RESOURCE_REF_SCHEMA_RESOURCE = "schemas/common/v1/resource-ref.schema.json"
RESOURCE_REF_MANIFEST_RESOURCE = "schemas/common/v1/resource-ref.manifest.json"
NEXUS_VFS_SOURCE_LOCK_RESOURCE = "schemas/common/v1/nexus-vfs.source-lock.json"
ZONE_ID_SCHEMA_RESOURCE = "schemas/common/v1/vendor/nexus-vfs/zone-id.schema.gen.json"
ZONE_PATH_SCHEMA_RESOURCE = "schemas/common/v1/vendor/nexus-vfs/zone-path.schema.gen.json"
ZONE_PATH_META_SCHEMA_RESOURCE = "schemas/common/v1/vendor/nexus-vfs/zone-path.meta-schema.gen.json"
RESOURCE_REF_PRIMITIVE_VALIDATION_AVAILABLE = True
RESOURCE_REF_DRAFT_FREEZE_AVAILABLE = True
MAX_RESOURCE_REF_JSON_BYTES = 65_536
MAX_EXTENSION_DEPTH = 8
MAX_EXTENSION_STRING_LENGTH = 4_096
MAX_EXTENSION_ITEMS = 64
MAX_EXTENSION_PROPERTIES = 32
MAX_RESOURCE_REF_PROPERTIES = 32
MAX_EXTENSION_NODES = MAX_RESOURCE_REF_JSON_BYTES
MIN_SAFE_JSON_INTEGER = -9_007_199_254_740_991
MAX_SAFE_JSON_INTEGER = 9_007_199_254_740_991

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
FrozenJsonValue: TypeAlias = (
    JsonScalar | tuple["FrozenJsonValue", ...] | Mapping[str, "FrozenJsonValue"]
)

_KNOWN_FIELDS = frozenset(
    {
        "api_version",
        "kind",
        "zone_id",
        "path",
        "version",
        "digest",
        "media_type",
        "size_bytes",
    }
)


@dataclass(frozen=True, slots=True)
class ResourceRefValidationIssue:
    """Stable, value-redacted validation diagnostic."""

    category: str
    path: str
    keyword: str
    message: str


class ResourceRefValidationError(ValidationError):
    """Raised when a ResourceRef fails structural or security validation."""

    def __init__(self, issues: tuple[ResourceRefValidationIssue, ...]):
        if not issues:
            raise ValueError("ResourceRefValidationError requires at least one issue")
        self.issues = issues
        summary = "; ".join(f"{issue.path}: {issue.category}" for issue in issues)
        super().__init__(f"ResourceRef validation failed: {summary}", path=issues[0].path)


class ResourceRefPrimitiveValidationUnavailable(ValidationError):
    """Raised when required primitive schema projections are unavailable."""

    def __init__(self) -> None:
        super().__init__("ResourceRef primitive schema projections are unavailable")


class ResourceRefDraftFreezeUnavailable(ValidationError):
    """Raised while the Nexus owner definition has no immutable revision."""

    def __init__(self) -> None:
        super().__init__(
            "ResourceRef draft-freeze is unavailable until the Nexus owner definition "
            "has an immutable revision"
        )


class _DuplicateKeyError(ValueError):
    pass


class _NonFiniteNumberError(ValueError):
    pass


class _FractionalNumberError(ValueError):
    pass


class _UnsafeIntegerError(ValueError):
    pass


class _JsonBudgetExceeded(ValueError):
    pass


class _UnsupportedJsonValue(ValueError):
    pass


def _resource_text(relative_path: str) -> str:
    return resources.files("nexus.contracts").joinpath(relative_path).read_text(encoding="utf-8")


@cache
def _canonical_schema() -> dict[str, Any]:
    schema = cast(dict[str, Any], json.loads(_resource_text(RESOURCE_REF_SCHEMA_RESOURCE)))
    Draft202012Validator.check_schema(schema)
    return schema


@cache
def _zone_id_schema() -> dict[str, Any]:
    schema = cast(dict[str, Any], json.loads(_resource_text(ZONE_ID_SCHEMA_RESOURCE)))
    Draft202012Validator.check_schema(schema)
    return schema


@cache
def _zone_path_schema() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(_resource_text(ZONE_PATH_SCHEMA_RESOURCE)))


@cache
def _zone_path_meta_schema() -> dict[str, Any]:
    schema = cast(dict[str, Any], json.loads(_resource_text(ZONE_PATH_META_SCHEMA_RESOURCE)))
    Draft202012Validator.check_schema(schema)
    return schema


def load_resource_ref_schema() -> dict[str, Any]:
    """Return a defensive copy of the canonical ResourceRef schema."""

    return copy.deepcopy(_canonical_schema())


def load_resource_ref_manifest() -> dict[str, Any]:
    """Load the adjacent owner manifest."""

    return cast(dict[str, Any], json.loads(_resource_text(RESOURCE_REF_MANIFEST_RESOURCE)))


def load_nexus_vfs_source_lock() -> dict[str, Any]:
    """Load the immutable nexus-vfs primitive source closure."""

    return cast(dict[str, Any], json.loads(_resource_text(NEXUS_VFS_SOURCE_LOCK_RESOURCE)))


def _validate_zone_path(
    _validator: Any, rules: object, instance: object, _schema: dict[str, Any]
) -> Iterator[JsonSchemaValidationError]:
    if not isinstance(instance, str) or not isinstance(rules, Mapping):
        return

    if cast(bool, rules["unicode_scalar_values_only"]) and any(
        0xD800 <= ord(character) <= 0xDFFF for character in instance
    ):
        yield JsonSchemaValidationError("must contain only Unicode scalar values")
        return
    if rules["empty"] == "reject" and not instance:
        yield JsonSchemaValidationError("must not be empty")
        return

    separator = cast(str, rules["separator"])
    if cast(bool, rules["absolute"]) and not instance.startswith(separator):
        yield JsonSchemaValidationError("must be absolute")
        return
    if instance == rules["root"]:
        return
    if rules["trailing_separator"] == "root-only" and instance.endswith(separator):
        yield JsonSchemaValidationError("must not end with a separator outside root")
        return
    if rules["repeated_separators"] == "reject" and separator * 2 in instance:
        yield JsonSchemaValidationError("must not contain repeated separators")
        return

    forbidden_characters = cast(list[str], rules["forbidden_characters"])
    if any(character in forbidden_characters for character in instance):
        yield JsonSchemaValidationError("contains a forbidden character")
        return

    forbidden_segments = cast(list[str], rules["forbidden_exact_segments"])
    if any(segment in forbidden_segments for segment in instance.split(separator)[1:]):
        yield JsonSchemaValidationError("contains a forbidden segment")


@cache
def _ensure_zone_path_dialect_registered() -> None:
    zone_path_schema = _zone_path_schema()
    zone_path_meta_schema = _zone_path_meta_schema()
    zone_path_validator: Any = validators.extend(
        Draft202012Validator,
        {"sudoZonePath": _validate_zone_path},
    )
    zone_path_validator.META_SCHEMA = copy.deepcopy(zone_path_meta_schema)
    validators.validates(cast(str, zone_path_meta_schema["$id"]))(zone_path_validator)
    zone_path_validator.check_schema(zone_path_schema)


@cache
def _reference_registry() -> Registry[Any]:
    _ensure_zone_path_dialect_registered()
    registry: Registry[Any] = Registry()
    for schema in (_zone_id_schema(), _zone_path_meta_schema(), _zone_path_schema()):
        registry = registry.with_resource(
            cast(str, schema["$id"]),
            cast(Any, Resource)(schema, DRAFT202012),
        )
    return registry


@cache
def _internal_resource_ref_validator() -> Draft202012Validator:
    return Draft202012Validator(_canonical_schema(), registry=_reference_registry())


def _isolated_reference_registry() -> Registry[Any]:
    _ensure_zone_path_dialect_registered()
    registry: Registry[Any] = Registry()
    for schema in (_zone_id_schema(), _zone_path_meta_schema(), _zone_path_schema()):
        isolated_schema = copy.deepcopy(schema)
        registry = registry.with_resource(
            cast(str, isolated_schema["$id"]),
            cast(Any, Resource)(isolated_schema, DRAFT202012),
        )
    return registry


def resource_ref_validator() -> Draft202012Validator:
    """Return an isolated structural validator with offline owner references.

    Whole-document byte, duplicate-key, and JSON-number policies are enforced by
    :func:`validate_resource_ref` and :meth:`ResourceRef.from_json`.
    """

    return Draft202012Validator(
        copy.deepcopy(_canonical_schema()),
        registry=_isolated_reference_registry(),
    )


def require_resource_ref_primitive_validation() -> None:
    """Fail closed unless both owner primitive projections are integrated."""

    if not RESOURCE_REF_PRIMITIVE_VALIDATION_AVAILABLE:
        raise ResourceRefPrimitiveValidationUnavailable


def require_resource_ref_draft_freeze() -> None:
    """Fail closed until the manifest can name immutable owner revisions."""

    if not RESOURCE_REF_DRAFT_FREEZE_AVAILABLE:
        raise ResourceRefDraftFreezeUnavailable


def _json_pointer(path: tuple[object, ...]) -> str:
    if not path:
        return ""
    escaped = (str(part).replace("~", "~0").replace("/", "~1") for part in path)
    return "/" + "/".join(escaped)


def _error_nodes(error: JsonSchemaValidationError) -> tuple[JsonSchemaValidationError, ...]:
    nodes = [error]
    for child in error.context:
        nodes.extend(_error_nodes(child))
    return tuple(nodes)


@cache
def _property_name_validator() -> Draft202012Validator:
    schema = copy.deepcopy(_canonical_schema()["$defs"]["safe_property_name"])
    return Draft202012Validator(schema)


def _property_name_issue(
    name: str, parent_path: tuple[object, ...]
) -> ResourceRefValidationIssue | None:
    errors = tuple(_property_name_validator().iter_errors(name))
    if not errors:
        return None
    nodes = tuple(node for error in errors for node in _error_nodes(error))
    is_secret_like = any(node.validator == "not" for node in nodes)
    return ResourceRefValidationIssue(
        category="secret_like_extension_key" if is_secret_like else "invalid_property_name",
        path=_json_pointer(parent_path),
        keyword="propertyNames",
        message=(
            "secret-like property names are forbidden in ResourceRef extensions"
            if is_secret_like
            else "property name is not an allowed snake_case extension name"
        ),
    )


def _issue_from_schema_error(error: JsonSchemaValidationError) -> ResourceRefValidationIssue:
    nodes = _error_nodes(error)
    path = tuple(error.absolute_path)
    keyword = str(error.validator)
    category = "schema_violation"
    message = "value does not satisfy the ResourceRef schema"

    secret_node = next(
        (
            node
            for node in nodes
            if node.validator == "not" and "propertyNames" in node.absolute_schema_path
        ),
        None,
    )
    large_extension_node = next(
        (
            node
            for node in nodes
            if node.validator in {"maxLength", "maxItems", "maxProperties"}
            and tuple(node.absolute_path)[:1] not in {(field,) for field in _KNOWN_FIELDS}
        ),
        None,
    )

    if secret_node is not None:
        category = "secret_like_extension_key"
        keyword = "propertyNames"
        path = tuple(secret_node.absolute_path)
        message = "secret-like property names are forbidden in ResourceRef extensions"
    elif large_extension_node is not None:
        category = "extension_value_too_large"
        keyword = str(large_extension_node.validator)
        path = tuple(large_extension_node.absolute_path)
        message = "ResourceRef extension value exceeds its structural limit"
    elif error.validator == "required":
        category = "missing_required"
        message = "a required ResourceRef property is absent"
    elif error.validator == "const" and path == ("api_version",):
        category = "unsupported_api_version"
        message = "api_version is not supported"
    elif error.validator == "const" and path == ("kind",):
        category = "unsupported_kind"
        message = "kind is not supported"
    elif error.validator == "type":
        category = "wrong_type"
        message = "property has the wrong JSON type"
    elif path == ("zone_id",):
        category = "invalid_zone_id"
        message = "zone_id does not satisfy the nexus-vfs ZoneId contract"
    elif path == ("path",):
        category = "invalid_zone_path"
        message = "path does not satisfy the nexus-vfs ZonePath contract"
    elif path == ("version",):
        category = "invalid_version"
        message = "version must be a non-empty opaque token"
    elif path == ("digest",):
        category = "invalid_digest"
        message = "digest must be algorithm-qualified"
    elif path == ("media_type",):
        category = "invalid_media_type"
        message = "media_type must be a canonical lowercase type/subtype"
    elif path == ("size_bytes",):
        category = "invalid_size_bytes"
        message = "size_bytes must be a canonical non-negative decimal string"
    elif error.validator == "propertyNames":
        category = "invalid_property_name"
        message = "property name is not an allowed snake_case extension name"

    return ResourceRefValidationIssue(
        category=category,
        path=_json_pointer(path),
        keyword=keyword,
        message=message,
    )


def _extension_resource_issue(
    value: object,
    *,
    depth: int,
    path: tuple[object, ...],
    node_budget: list[int],
) -> ResourceRefValidationIssue | None:
    node_budget[0] += 1
    if node_budget[0] > MAX_EXTENSION_NODES:
        return _document_too_large_issue()
    if isinstance(value, Mapping):
        if depth >= MAX_EXTENSION_DEPTH:
            return ResourceRefValidationIssue(
                category="extension_too_deep",
                path=_json_pointer(path),
                keyword="maxDepth",
                message="ResourceRef extension nesting exceeds its limit",
            )
        if len(value) > MAX_EXTENSION_PROPERTIES:
            return ResourceRefValidationIssue(
                category="extension_value_too_large",
                path=_json_pointer(path),
                keyword="maxProperties",
                message="ResourceRef extension value exceeds its structural limit",
            )
        for key, child in value.items():
            if not isinstance(key, str):
                return ResourceRefValidationIssue(
                    category="invalid_property_name",
                    path=_json_pointer(path),
                    keyword="propertyNames",
                    message="ResourceRef extension property names must be strings",
                )
            name_issue = _property_name_issue(key, path)
            if name_issue is not None:
                return name_issue
            issue = _extension_resource_issue(
                child,
                depth=depth + 1,
                path=(*path, key),
                node_budget=node_budget,
            )
            if issue is not None:
                return issue
    elif isinstance(value, (list, tuple)):
        if depth >= MAX_EXTENSION_DEPTH:
            return ResourceRefValidationIssue(
                category="extension_too_deep",
                path=_json_pointer(path),
                keyword="maxDepth",
                message="ResourceRef extension nesting exceeds its limit",
            )
        if len(value) > MAX_EXTENSION_ITEMS:
            return ResourceRefValidationIssue(
                category="extension_value_too_large",
                path=_json_pointer(path),
                keyword="maxItems",
                message="ResourceRef extension value exceeds its structural limit",
            )
        for index, child in enumerate(value):
            issue = _extension_resource_issue(
                child,
                depth=depth + 1,
                path=(*path, index),
                node_budget=node_budget,
            )
            if issue is not None:
                return issue
    elif isinstance(value, str) and len(value) > MAX_EXTENSION_STRING_LENGTH:
        return ResourceRefValidationIssue(
            category="extension_value_too_large",
            path=_json_pointer(path),
            keyword="maxLength",
            message="ResourceRef extension value exceeds its structural limit",
        )
    elif isinstance(value, int) and not isinstance(value, bool):
        if not MIN_SAFE_JSON_INTEGER <= value <= MAX_SAFE_JSON_INTEGER:
            return ResourceRefValidationIssue(
                category="unsafe_extension_integer",
                path=_json_pointer(path),
                keyword="number",
                message="ResourceRef extension integers must use the JSON-safe range",
            )
    elif isinstance(value, float):
        if not math.isfinite(value):
            return ResourceRefValidationIssue(
                category="non_finite_number",
                path=_json_pointer(path),
                keyword="number",
                message="ResourceRef JSON contains a non-finite number",
            )
        if not value.is_integer():
            return ResourceRefValidationIssue(
                category="fractional_extension_number",
                path=_json_pointer(path),
                keyword="number",
                message="ResourceRef extension numbers must represent safe integers",
            )
        if not MIN_SAFE_JSON_INTEGER <= value <= MAX_SAFE_JSON_INTEGER:
            return ResourceRefValidationIssue(
                category="unsafe_extension_integer",
                path=_json_pointer(path),
                keyword="number",
                message="ResourceRef extension integers must use the JSON-safe range",
            )
    return None


def _extensions_resource_issue(
    extensions: Mapping[str, object],
) -> ResourceRefValidationIssue | None:
    node_budget = [0]
    for key, value in extensions.items():
        if not isinstance(key, str):
            return ResourceRefValidationIssue(
                category="invalid_property_name",
                path="",
                keyword="propertyNames",
                message="ResourceRef extension property names must be strings",
            )
        name_issue = _property_name_issue(key, ())
        if name_issue is not None:
            return name_issue
        issue = _extension_resource_issue(
            value,
            depth=0,
            path=(key,),
            node_budget=node_budget,
        )
        if issue is not None:
            return issue
    return None


def _zone_path_preflight_issue(value: str) -> ResourceRefValidationIssue | None:
    rules = _zone_path_schema().get("sudoZonePath")
    error = next(_validate_zone_path(None, rules, value, {}), None)
    if error is None:
        return None
    return ResourceRefValidationIssue(
        category="invalid_zone_path",
        path="/path",
        keyword="sudoZonePath",
        message="path does not satisfy the nexus-vfs ZonePath contract",
    )


def validate_resource_ref(value: object) -> tuple[ResourceRefValidationIssue, ...]:
    """Validate the complete provisional owner policy without claiming primitive conformance."""

    if not isinstance(value, dict):
        return (
            ResourceRefValidationIssue(
                category="wrong_type",
                path="",
                keyword="type",
                message="ResourceRef must be a JSON object",
            ),
        )

    if len(value) > MAX_RESOURCE_REF_PROPERTIES:
        return (
            ResourceRefValidationIssue(
                category="too_many_properties",
                path="",
                keyword="maxProperties",
                message="ResourceRef contains too many properties",
            ),
        )

    extensions: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            return (
                ResourceRefValidationIssue(
                    category="invalid_property_name",
                    path="",
                    keyword="propertyNames",
                    message="ResourceRef property names must be strings",
                ),
            )
        if key in _KNOWN_FIELDS:
            if not isinstance(item, str):
                return (
                    ResourceRefValidationIssue(
                        category="wrong_type",
                        path=_json_pointer((key,)),
                        keyword="type",
                        message="property has the wrong JSON type",
                    ),
                )
            continue
        name_issue = _property_name_issue(key, ())
        if name_issue is not None:
            return (name_issue,)
        extensions[key] = item

    resource_issue = _extensions_resource_issue(extensions)
    if resource_issue is not None:
        return (resource_issue,)

    path_value = value.get("path")
    if isinstance(path_value, str):
        if len(path_value) > MAX_RESOURCE_REF_JSON_BYTES:
            return (_document_too_large_issue(),)
        path_issue = _zone_path_preflight_issue(path_value)
        if path_issue is not None:
            return (path_issue,)

    encoding_issue = _encoded_value_issue(value)
    if encoding_issue is not None:
        return (encoding_issue,)

    try:
        errors = sorted(
            _internal_resource_ref_validator().iter_errors(value),
            key=lambda error: list(error.absolute_path),
        )
    except RecursionError:
        return (
            ResourceRefValidationIssue(
                category="extension_too_deep",
                path="",
                keyword="maxDepth",
                message="ResourceRef extension nesting exceeds its limit",
            ),
        )
    issues = [_issue_from_schema_error(error) for error in errors]
    if issues:
        unique = {
            (issue.category, issue.path, issue.keyword, issue.message): issue for issue in issues
        }
        return tuple(sorted(unique.values(), key=lambda issue: (issue.path, issue.category)))
    return ()


def _freeze(value: object) -> FrozenJsonValue:
    if isinstance(value, Mapping):
        return types.MappingProxyType({str(key): _freeze(child) for key, child in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(child) for child in value)
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return cast(FrozenJsonValue, value)


def _thaw(value: FrozenJsonValue) -> JsonValue:
    if isinstance(value, tuple):
        return [_thaw(child) for child in value]
    if isinstance(value, Mapping):
        return {key: _thaw(child) for key, child in value.items()}
    return cast(JsonValue, value)


def _reject_duplicate_keys(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError
        result[key] = value
    return result


def _reject_non_finite_number(_value: str) -> None:
    raise _NonFiniteNumberError


def _parse_json_integer(value: str) -> int:
    digits = value.removeprefix("-")
    if len(digits) > 16:
        raise _UnsafeIntegerError
    parsed = int(value)
    if not MIN_SAFE_JSON_INTEGER <= parsed <= MAX_SAFE_JSON_INTEGER:
        raise _UnsafeIntegerError
    return parsed


def _parse_json_float(value: str) -> int:
    try:
        parsed = Decimal(value)
        integral = parsed.to_integral_value()
        if parsed != integral:
            raise _FractionalNumberError
        if not Decimal(MIN_SAFE_JSON_INTEGER) <= parsed <= Decimal(MAX_SAFE_JSON_INTEGER):
            raise _UnsafeIntegerError
        return int(integral)
    except InvalidOperation:
        raise _UnsafeIntegerError from None


def _document_too_large_issue() -> ResourceRefValidationIssue:
    return ResourceRefValidationIssue(
        category="document_too_large",
        path="",
        keyword="maxBytes",
        message="ResourceRef JSON exceeds the document-size limit",
    )


def _decode_json(payload: str | bytes) -> object:
    if isinstance(payload, bytes):
        if len(payload) > MAX_RESOURCE_REF_JSON_BYTES:
            raise ResourceRefValidationError((_document_too_large_issue(),))
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            issue = ResourceRefValidationIssue(
                category="invalid_utf8",
                path="",
                keyword="encoding",
                message="ResourceRef JSON must be UTF-8",
            )
            raise ResourceRefValidationError((issue,)) from None
    else:
        if len(payload) > MAX_RESOURCE_REF_JSON_BYTES:
            raise ResourceRefValidationError((_document_too_large_issue(),))
        text = payload
        try:
            raw = payload.encode("utf-8")
        except UnicodeEncodeError:
            issue = ResourceRefValidationIssue(
                category="invalid_utf8",
                path="",
                keyword="encoding",
                message="ResourceRef JSON must be UTF-8",
            )
            raise ResourceRefValidationError((issue,)) from None
        if len(raw) > MAX_RESOURCE_REF_JSON_BYTES:
            raise ResourceRefValidationError((_document_too_large_issue(),))

    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite_number,
            parse_int=_parse_json_integer,
            parse_float=_parse_json_float,
        )
    except _DuplicateKeyError:
        issue = ResourceRefValidationIssue(
            category="duplicate_property",
            path="",
            keyword="uniqueKeys",
            message="ResourceRef JSON contains a duplicate property",
        )
        raise ResourceRefValidationError((issue,)) from None
    except _NonFiniteNumberError:
        issue = ResourceRefValidationIssue(
            category="non_finite_number",
            path="",
            keyword="number",
            message="ResourceRef JSON contains a non-finite number",
        )
        raise ResourceRefValidationError((issue,)) from None
    except _FractionalNumberError:
        issue = ResourceRefValidationIssue(
            category="fractional_extension_number",
            path="",
            keyword="number",
            message="ResourceRef extension numbers must represent safe integers",
        )
        raise ResourceRefValidationError((issue,)) from None
    except _UnsafeIntegerError:
        issue = ResourceRefValidationIssue(
            category="unsafe_extension_integer",
            path="",
            keyword="number",
            message="ResourceRef extension integers must use the JSON-safe range",
        )
        raise ResourceRefValidationError((issue,)) from None
    except json.JSONDecodeError:
        issue = ResourceRefValidationIssue(
            category="invalid_json",
            path="",
            keyword="parse",
            message="ResourceRef payload is not valid JSON",
        )
        raise ResourceRefValidationError((issue,)) from None


def _charge_json_bytes(size: int, used: list[int]) -> None:
    used[0] += size
    if used[0] > MAX_RESOURCE_REF_JSON_BYTES:
        raise _JsonBudgetExceeded


def _measure_json_value(value: object, used: list[int]) -> None:
    if isinstance(value, Mapping):
        _charge_json_bytes(2, used)
        for index, (key, child) in enumerate(value.items()):
            if not isinstance(key, str):
                raise _UnsupportedJsonValue
            if index:
                _charge_json_bytes(1, used)
            _measure_json_value(key, used)
            _charge_json_bytes(1, used)
            _measure_json_value(child, used)
        return
    if isinstance(value, (list, tuple)):
        _charge_json_bytes(2, used)
        for index, child in enumerate(value):
            if index:
                _charge_json_bytes(1, used)
            _measure_json_value(child, used)
        return
    if isinstance(value, str):
        if len(value) > MAX_RESOURCE_REF_JSON_BYTES - used[0]:
            raise _JsonBudgetExceeded
        encoded = json.dumps(value, ensure_ascii=False).encode("utf-8")
        _charge_json_bytes(len(encoded), used)
        return
    if value is None:
        _charge_json_bytes(4, used)
        return
    if isinstance(value, bool):
        _charge_json_bytes(4 if value else 5, used)
        return
    if isinstance(value, int):
        if not MIN_SAFE_JSON_INTEGER <= value <= MAX_SAFE_JSON_INTEGER:
            raise _UnsupportedJsonValue
        _charge_json_bytes(len(str(value)), used)
        return
    if isinstance(value, float):
        if (
            not math.isfinite(value)
            or not value.is_integer()
            or not MIN_SAFE_JSON_INTEGER <= value <= MAX_SAFE_JSON_INTEGER
        ):
            raise _UnsupportedJsonValue
        _charge_json_bytes(len(json.dumps(value, allow_nan=False)), used)
        return
    raise _UnsupportedJsonValue


def _encoded_value_issue(value: object) -> ResourceRefValidationIssue | None:
    try:
        _measure_json_value(value, [0])
    except _JsonBudgetExceeded:
        return _document_too_large_issue()
    except (_UnsupportedJsonValue, UnicodeEncodeError, RecursionError):
        return ResourceRefValidationIssue(
            category="not_json_value",
            path="",
            keyword="json",
            message="ResourceRef input must contain only finite JSON values",
        )
    return None


def _validate_or_raise(value: object) -> None:
    issues = validate_resource_ref(value)
    if issues:
        raise ResourceRefValidationError(issues)


@dataclass(frozen=True, slots=True)
class ResourceRef:
    """Parsed Product ResourceRef with lossless unknown-optional preservation.

    The object is a locator, not a rename-stable identity or authorization
    grant. Construction validates the draft-frozen owner envelope, extension policy,
    and exact-pinned primitive projections. Callers may use
    :func:`require_resource_ref_draft_freeze` to assert that owner provenance is active.
    """

    zone_id: str
    path: str
    api_version: str = RESOURCE_REF_API_VERSION
    kind: str = RESOURCE_REF_KIND
    version: str | None = None
    digest: str | None = None
    media_type: str | None = None
    size_bytes: str | None = None
    additional_properties: Mapping[str, FrozenJsonValue] = field(
        default_factory=lambda: types.MappingProxyType({}), repr=False
    )

    def __post_init__(self) -> None:
        collisions = _KNOWN_FIELDS.intersection(self.additional_properties)
        if collisions:
            issue = ResourceRefValidationIssue(
                category="known_property_collision",
                path="",
                keyword="properties",
                message="additional properties cannot replace known ResourceRef properties",
            )
            raise ResourceRefValidationError((issue,))

        known_property_count = 4 + sum(
            value is not None
            for value in (self.version, self.digest, self.media_type, self.size_bytes)
        )
        if known_property_count + len(self.additional_properties) > MAX_RESOURCE_REF_PROPERTIES:
            issue = ResourceRefValidationIssue(
                category="too_many_properties",
                path="",
                keyword="maxProperties",
                message="ResourceRef contains too many properties",
            )
            raise ResourceRefValidationError((issue,))

        resource_issue = _extensions_resource_issue(self.additional_properties)
        if resource_issue is not None:
            raise ResourceRefValidationError((resource_issue,))
        raw_wire: dict[str, object] = {
            "api_version": self.api_version,
            "kind": self.kind,
            "zone_id": self.zone_id,
            "path": self.path,
        }
        if self.version is not None:
            raw_wire["version"] = self.version
        if self.digest is not None:
            raw_wire["digest"] = self.digest
        if self.media_type is not None:
            raw_wire["media_type"] = self.media_type
        if self.size_bytes is not None:
            raw_wire["size_bytes"] = self.size_bytes
        raw_wire.update(self.additional_properties)
        encoding_issue = _encoded_value_issue(raw_wire)
        if encoding_issue is not None:
            raise ResourceRefValidationError((encoding_issue,))

        frozen_extensions = types.MappingProxyType(
            {key: _freeze(value) for key, value in self.additional_properties.items()}
        )
        object.__setattr__(self, "additional_properties", frozen_extensions)
        _validate_or_raise(self.to_dict())

    @classmethod
    def from_dict(cls, value: dict[str, JsonValue]) -> "ResourceRef":
        """Validate and parse a JSON-compatible dictionary."""

        _validate_or_raise(value)
        extras = {key: item for key, item in value.items() if key not in _KNOWN_FIELDS}
        instance = object.__new__(cls)
        object.__setattr__(instance, "zone_id", cast(str, value["zone_id"]))
        object.__setattr__(instance, "path", cast(str, value["path"]))
        object.__setattr__(instance, "api_version", cast(str, value["api_version"]))
        object.__setattr__(instance, "kind", cast(str, value["kind"]))
        object.__setattr__(instance, "version", cast(str | None, value.get("version")))
        object.__setattr__(instance, "digest", cast(str | None, value.get("digest")))
        object.__setattr__(instance, "media_type", cast(str | None, value.get("media_type")))
        object.__setattr__(instance, "size_bytes", cast(str | None, value.get("size_bytes")))
        object.__setattr__(
            instance,
            "additional_properties",
            types.MappingProxyType({key: _freeze(item) for key, item in extras.items()}),
        )
        return instance

    @classmethod
    def from_json(cls, payload: str | bytes) -> "ResourceRef":
        """Parse UTF-8 JSON, rejecting duplicates and non-finite numbers."""

        value = _decode_json(payload)
        if not isinstance(value, dict):
            issues = validate_resource_ref(value)
            raise ResourceRefValidationError(issues)
        return cls.from_dict(cast(dict[str, JsonValue], value))

    def to_dict(self) -> dict[str, JsonValue]:
        """Serialize to canonical wire keys while omitting absent optionals."""

        result: dict[str, JsonValue] = {
            "api_version": self.api_version,
            "kind": self.kind,
            "zone_id": self.zone_id,
            "path": self.path,
        }
        if self.version is not None:
            result["version"] = self.version
        if self.digest is not None:
            result["digest"] = self.digest
        if self.media_type is not None:
            result["media_type"] = self.media_type
        if self.size_bytes is not None:
            result["size_bytes"] = self.size_bytes
        result.update({key: _thaw(value) for key, value in self.additional_properties.items()})
        return result

    def to_json(self) -> str:
        """Serialize through the real JSON encoder with deterministic keys."""

        return json.dumps(
            self.to_dict(),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
