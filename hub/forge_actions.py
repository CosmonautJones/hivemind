"""Allowlisted system-lane maintenance actions (catalog = hub/actions.json, shared with agent/actions.ps1)."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

ACTIONS_FILE = Path(__file__).resolve().parent / "actions.json"
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


class ActionError(ValueError):
    pass


def load_catalog(path: Path = ACTIONS_FILE) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def find_action(catalog: dict, name: Any) -> Optional[dict]:
    if not isinstance(name, str):
        return None
    return next((a for a in catalog.get("actions", []) if a.get("name") == name), None)


def validate_args(spec: dict, args: Any) -> dict:
    """Validate `args` against an action spec; returns args with defaults applied. Raises ActionError."""
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ActionError("args must be an object")
    schema: dict = spec.get("args") or {}
    unknown = sorted(set(args) - set(schema))
    if unknown:
        raise ActionError(f"unknown arg(s) for {spec['name']}: {', '.join(unknown)}")
    out: dict = {}
    for key, rule in schema.items():
        val = args.get(key)
        if val is None or val == "":
            if rule.get("required"):
                raise ActionError(f"{spec['name']}: '{key}' is required")
            if "default" in rule:
                out[key] = rule["default"]
            continue
        typ = rule.get("type", "string")
        if typ == "int":
            if isinstance(val, bool) or not isinstance(val, int):
                raise ActionError(f"{spec['name']}: '{key}' must be an integer")
            if "min" in rule and val < rule["min"] or "max" in rule and val > rule["max"]:
                raise ActionError(f"{spec['name']}: '{key}' must be between {rule.get('min')} and {rule.get('max')}")
        elif typ == "bool":                      # 2.3 (forge_uninstall keep_data)
            if not isinstance(val, bool):
                raise ActionError(f"{spec['name']}: '{key}' must be true or false")
        elif typ == "string":
            if not isinstance(val, str):
                raise ActionError(f"{spec['name']}: '{key}' must be a string")
            if _CTRL.search(val):
                raise ActionError(f"{spec['name']}: '{key}' contains control characters")
            if len(val) > int(rule.get("max_len", 512)):
                raise ActionError(f"{spec['name']}: '{key}' is too long")
            if "enum" in rule and val not in rule["enum"]:
                raise ActionError(f"{spec['name']}: '{key}' must be one of {rule['enum']}")
            if "pattern" in rule and not re.fullmatch(rule["pattern"], val):
                raise ActionError(f"{spec['name']}: '{key}' has invalid characters")
            if any(val.lower() == x.lower() for x in rule.get("not_in", [])):
                raise ActionError(f"{spec['name']}: '{key}' may not be '{val}'")
        else:
            raise ActionError(f"{spec['name']}: unsupported arg type {typ}")
        out[key] = val
    return out


def action_lane(spec: Optional[dict]) -> str:
    """'user' for actions that run in the node user's agent (2.3 F2), else 'system' (the default)."""
    return "user" if (spec or {}).get("lane") == "user" else "system"


def summary(name: str, args: dict) -> str:
    """Human-readable one-liner stored as the job prompt."""
    return f"{name} {json.dumps(args, sort_keys=True)}" if args else name
