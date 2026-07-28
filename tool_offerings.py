"""Provider-neutral validation for persisted mixed tool offerings.

Praxis stores the exact model tool list in durable checkpoints.  That list can
contain both locally dispatched function tools and a provider-executed hosted
web-search descriptor.  Recovery must validate the same shape as the live loop
without ever treating the hosted tool as a locally replayable function.
"""

from __future__ import annotations

from collections.abc import Iterable


class ToolOfferingError(ValueError):
    """One offered descriptor is malformed or aliases another tool identity."""


def local_function_names(tools: Iterable[object]) -> frozenset[str]:
    """Return locally dispatchable names after strict mixed-list validation."""

    names: set[str] = set()
    identities: set[str] = set()
    for index, schema in enumerate(tools):
        if not isinstance(schema, dict):
            raise ToolOfferingError(f"offered tool[{index}] is malformed or duplicated")
        server_type = str(schema.get("type") or "")
        if server_type in {"web_search", "web_search_20250305"}:
            allowed_keys = ({"type", "search_context_size", "external_web_access", "max_uses"}
                            if server_type == "web_search"
                            else {"type", "name", "max_uses"})
            malformed = bool(set(schema) - allowed_keys)
            if server_type == "web_search":
                malformed = malformed or "name" in schema or "input_schema" in schema
                context_size = schema.get("search_context_size")
                malformed = malformed or (
                    context_size is not None
                    and (not isinstance(context_size, str)
                         or context_size not in {"low", "medium", "high"})
                )
                external = schema.get("external_web_access")
                malformed = malformed or (
                    external is not None and not isinstance(external, bool)
                )
            else:
                malformed = (malformed or schema.get("name") != "web_search"
                             or "input_schema" in schema)
            max_uses = schema.get("max_uses")
            malformed = malformed or (
                max_uses is not None
                and (isinstance(max_uses, bool) or not isinstance(max_uses, int)
                     or not 1 <= max_uses <= 10)
            )
            if malformed:
                raise ToolOfferingError(
                    f"offered tool[{index}] is malformed or duplicated")
            identity = "web_search"
            if identity in identities:
                raise ToolOfferingError(
                    f"offered tool[{index}] is malformed or duplicated")
            identities.add(identity)
            continue
        if "type" in schema:
            raise ToolOfferingError(f"offered tool[{index}] is malformed or duplicated")
        name = schema.get("name")
        if (not isinstance(name, str) or not name or name != name.strip()
                or name in identities):
            raise ToolOfferingError(f"offered tool[{index}] is malformed or duplicated")
        identities.add(name)
        names.add(name)
    return frozenset(names)


__all__ = ["ToolOfferingError", "local_function_names"]
