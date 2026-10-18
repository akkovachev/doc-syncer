"""YAML configuration loader and JSON Schema validator (`/tools/common/config.py`).

Implements Section 4 of the Architecture Proposal:
- Validates `.doc-sync.yml` (or any tool module config) against its JSON Schema
  before performing any network I/O so malformed configs fail fast with a precise error.
- Uses `jsonschema` when installed, with a built-in zero-dependency Draft-7/2020-12
  schema validator fallback so validation works in every Python environment.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Union

import yaml

try:
    import jsonschema  # type: ignore[import-untyped]
except ImportError:
    jsonschema = None


class ConfigValidationError(ValueError):
    """Raised when a configuration file is missing, invalid YAML, or fails schema validation."""

    def __init__(self, message: str, errors: Optional[List[str]] = None) -> None:
        super().__init__(message)
        self.errors = errors or [message]


from typing import Optional  # noqa: E402


def load_yaml_file(path: Union[str, Path]) -> Dict[str, Any]:
    """Load a YAML file safely and verify the top-level document is a mapping."""
    file_path = Path(path)
    if not file_path.exists():
        raise ConfigValidationError(f"Configuration file not found: {file_path}")
    try:
        raw_text = file_path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ConfigValidationError(f"Invalid YAML syntax in {file_path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigValidationError(
            f"Configuration root in {file_path} must be a YAML mapping (object), got {type(data).__name__}."
        )
    return data


def _matches_type(value: Any, expected_type: str) -> bool:
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected_type == "null":
        return value is None
    return True


def _builtin_validate(instance: Any, schema: Dict[str, Any], path: str = "$") -> List[str]:
    """Built-in recursive JSON Schema validator supporting Draft-7 / 2020-12 constructs."""
    errors: List[str] = []

    if "type" in schema:
        expected = schema["type"]
        types = [expected] if isinstance(expected, str) else expected
        if not any(_matches_type(instance, t) for t in types):
            errors.append(f"{path}: expected type '{expected}', got {type(instance).__name__} ({instance!r})")
            return errors

    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: expected constant value {schema['const']!r}, got {instance!r}")

    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: value {instance!r} must be one of {schema['enum']}")

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < int(schema["minLength"]):
            errors.append(f"{path}: string length {len(instance)} is less than minLength {schema['minLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            errors.append(f"{path}: string {instance!r} does not match pattern {schema['pattern']!r}")

    if isinstance(instance, dict):
        for req_key in schema.get("required", []):
            if req_key not in instance:
                errors.append(f"{path}: missing required property '{req_key}'")

        props = schema.get("properties", {})
        add_props = schema.get("additionalProperties", True)
        for key, val in instance.items():
            subpath = f"{path}.{key}"
            if key in props:
                errors.extend(_builtin_validate(val, props[key], subpath))
            elif add_props is False:
                errors.append(f"{path}: unknown property '{key}' is not allowed")
            elif isinstance(add_props, dict):
                errors.extend(_builtin_validate(val, add_props, subpath))

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < int(schema["minItems"]):
            errors.append(f"{path}: array must contain at least {schema['minItems']} item(s)")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for idx, item in enumerate(instance):
                errors.extend(_builtin_validate(item, item_schema, f"{path}[{idx}]"))

    if "anyOf" in schema:
        sub_schemas = schema["anyOf"]
        if not any(len(_builtin_validate(instance, sub, path)) == 0 for sub in sub_schemas):
            errors.append(f"{path}: did not satisfy anyOf constraints")

    if "oneOf" in schema:
        sub_schemas = schema["oneOf"]
        matching = sum(1 for sub in sub_schemas if len(_builtin_validate(instance, sub, path)) == 0)
        if matching != 1:
            errors.append(
                f"{path}: must satisfy exactly one of the oneOf constraints (matched {matching})"
            )

    return errors


def validate_against_schema(data: Dict[str, Any], schema: Dict[str, Any]) -> None:
    """Validate a parsed configuration dictionary against a JSON Schema."""
    errors: List[str] = []

    if jsonschema is not None:
        validator_cls = jsonschema.validators.validator_for(schema)
        validator = validator_cls(schema)
        for err in sorted(validator.iter_errors(data), key=lambda e: list(e.path)):
            loc = "$." + ".".join(str(p) for p in err.path) if err.path else "$"
            errors.append(f"{loc}: {err.message}")
    else:
        errors = _builtin_validate(data, schema, path="$")

    if errors:
        formatted = "\n  - ".join(errors)
        raise ConfigValidationError(
            f"Configuration schema validation failed with {len(errors)} error(s):\n  - {formatted}",
            errors=errors,
        )


def load_and_validate_config(
    config_path: Union[str, Path],
    schema_path: Union[str, Path],
) -> Dict[str, Any]:
    """Load a YAML configuration file and validate it against a JSON Schema file."""
    s_path = Path(schema_path)
    if not s_path.exists():
        raise ConfigValidationError(f"JSON Schema file not found: {s_path}")
    schema = json.loads(s_path.read_text(encoding="utf-8"))
    data = load_yaml_file(config_path)
    validate_against_schema(data, schema)
    return data
