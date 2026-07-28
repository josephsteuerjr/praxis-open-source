"""Searchable Telethon TL registry and account-call dispatcher.

PASS 24 deliberately keeps this module independent from ``mtproto_runner`` and from
the Praxis agent.  It discovers the *installed* Telethon schema at runtime and
accepts an injected async caller/entity resolver.  Consequently it can be tested
without a Telegram session and cannot quietly create a second client or brain.

The generic ``raw`` path is available only to sovereign principals: the human owner
and Praxis herself.  Account-critical/auth/session constructors additionally require
a separately issued, request-and-parameters-bound owner confirmation proof checked by
an injected verifier.  Transport wrapper requests are
visible in the registry (schema completeness) but are never dispatchable: accepting
``InvokeWith*`` would let a nested request bypass the policy applied here.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import inspect
import json
import os
import pkgutil
import re
import time
import types as pytypes
import typing
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Mapping, Protocol


SCOPES = (
    "telegram.read",
    "telegram.communicate",
    "telegram.moderate",
    "telegram.membership",
    "telegram.account",
)

_MISSING = object()
# Deliberately process-ephemeral.  Account-critical arguments may contain a
# password or login code, so a durable/model-visible plain digest would be an
# offline verifier for a small secret.  The commitment can correlate one live
# confirmation flow, but becomes unverifiable after process restart; recovery
# obtains the exact arguments only from the authenticated encrypted spool.
_PARAMETER_COMMITMENT_KEY = os.urandom(32)
_SAFE_ERROR_TYPE_RE = re.compile(
    r"(?:[A-Za-z_][A-Za-z0-9_]*\.)*[A-Za-z_][A-Za-z0-9_]*(?:Error|Exception|Failure)"
)
_SAFE_CRITICAL_POLICY_TYPES = frozenset({
    "ConfirmationRequired", "ConfirmationRejected", "PermissionDenied",
})
_CAMEL = re.compile(r"(?<!^)(?=[A-Z])")
_ENTITY_TYPES = {
    "InputPeer",
    "TypeInputPeer",
    "InputChannel",
    "TypeInputChannel",
    "InputUser",
    "TypeInputUser",
    "InputDialogPeer",
    "TypeInputDialogPeer",
    "InputNotifyPeer",
    "TypeInputNotifyPeer",
}


class TelethonSchemaUnavailable(RuntimeError):
    """The pinned Telethon package cannot be imported."""


class RegistryLookupError(LookupError):
    """An exact TL request name was not found or was ambiguous."""


class ParameterValidationError(ValueError):
    """Parameters do not match the installed request constructor."""


class EntityResolver(Protocol):
    def __call__(
        self,
        value: Any,
        expected_type: str,
        field: str,
        request_name: str,
    ) -> Any | Awaitable[Any]: ...


class AsyncRequestCaller(Protocol):
    def __call__(self, request: Any) -> Any | Awaitable[Any]: ...


@dataclass(frozen=True)
class ParameterDescriptor:
    name: str
    type_name: str
    required: bool
    default: Any = field(default=_MISSING, repr=False)
    entity: bool = False
    annotation: Any = field(default=inspect.Signature.empty, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        result = {
            "name": self.name,
            "type": self.type_name,
            "required": self.required,
            "entity": self.entity,
        }
        if self.default is not _MISSING:
            result["default"] = to_jsonable(self.default)
        return result


@dataclass(frozen=True)
class RequestDescriptor:
    name: str
    namespace: str
    request_class: type = field(repr=False, compare=False)
    constructor_id: int
    result_type_id: int | None
    scope: str
    risk: str
    policy_reason: str
    parameters: tuple[ParameterDescriptor, ...]

    @property
    def callable(self) -> bool:
        return self.risk != "transport_internal"

    @property
    def requires_confirmation(self) -> bool:
        return self.risk == "account_critical"

    @property
    def short_name(self) -> str:
        return self.name.rsplit(".", 1)[-1]

    @property
    def schema(self) -> dict[str, Any]:
        properties: dict[str, Any] = {}
        required: list[str] = []
        for parameter in self.parameters:
            properties[parameter.name] = _annotation_schema(
                parameter.annotation, entity=parameter.entity
            )
            if parameter.default is not _MISSING:
                properties[parameter.name]["default"] = to_jsonable(parameter.default)
            if parameter.required:
                required.append(parameter.name)
        schema: dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "additionalProperties": False,
        }
        if required:
            schema["required"] = required
        return schema

    def to_dict(self, *, detailed: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "name": self.name,
            "short_name": self.short_name,
            "namespace": self.namespace,
            "constructor_id": self.constructor_id,
            "constructor_hex": f"0x{self.constructor_id:08x}",
            "result_type_id": self.result_type_id,
            "scope": self.scope,
            "risk": self.risk,
            "callable": self.callable,
            "raw_sovereign_only": True,
            "requires_confirmation": self.requires_confirmation,
            "policy_reason": self.policy_reason,
        }
        if detailed:
            result["parameters"] = [parameter.to_dict() for parameter in self.parameters]
            result["schema"] = self.schema
        return result


@dataclass(frozen=True)
class ConfirmationBinding:
    request_name: str
    parameter_commitment: str
    principal: str
    scope: str

    def to_dict(self) -> dict[str, str]:
        return {
            "request_name": self.request_name,
            "binding_id": self.parameter_commitment,
            "principal": self.principal,
            "scope": self.scope,
        }


@dataclass(frozen=True, slots=True)
class CriticalConfirmation:
    """Opaque proof issued by a separate owner-confirmation workflow.

    Merely constructing this value is insufficient.  The dispatcher only accepts it
    when its injected verifier validates and consumes ``token`` against the current
    process-local binding.  No parameter verifier is carried in the proof itself.
    """

    token: str = field(repr=False)


@dataclass(frozen=True)
class DispatchReceipt:
    receipt_id: str
    status: str
    request_name: str
    scope: str | None
    mode: str
    principal: str
    started_at: str
    finished_at: str
    duration_ms: int
    constructor_id: int | None = None
    submitted_parameters: Any = None
    serialized_parameters: Any = None
    parameter_commitment: str | None = None
    result: Any = None
    result_sha256: str | None = None
    identifiers: Mapping[str, list[int]] = field(default_factory=dict)
    error: Mapping[str, Any] | None = None
    retry: Mapping[str, Any] | None = None
    policy: Mapping[str, Any] = field(default_factory=dict)
    delivery_context: Any = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(
            {
                "receipt_id": self.receipt_id,
                "status": self.status,
                "request_name": self.request_name,
                "scope": self.scope,
                "mode": self.mode,
                "principal": self.principal,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "duration_ms": self.duration_ms,
                "constructor_id": self.constructor_id,
                "submitted_parameters": self.submitted_parameters,
                "serialized_parameters": self.serialized_parameters,
                "parameter_commitment": self.parameter_commitment,
                "result": self.result,
                "result_sha256": self.result_sha256,
                "identifiers": dict(self.identifiers),
                "error": self.error,
                "retry": self.retry,
                "policy": dict(self.policy),
                "delivery_context": self.delivery_context,
            }
        )


def _load_telethon() -> tuple[Any, Any, type, type, str, int | None]:
    try:
        import telethon
        from telethon.tl import alltlobjects, functions, types
        from telethon.tl.tlobject import TLObject, TLRequest
    except Exception as exc:  # pragma: no cover - exercised only in broken installs
        raise TelethonSchemaUnavailable(f"Telethon schema unavailable: {exc}") from exc
    return (
        functions,
        types,
        TLObject,
        TLRequest,
        str(getattr(telethon, "__version__", "unknown")),
        getattr(alltlobjects, "LAYER", None),
    )


def _iter_request_classes(functions: Any, tl_request: type) -> Iterable[type]:
    modules = [functions]
    package_path = getattr(functions, "__path__", None)
    if package_path:
        for item in pkgutil.iter_modules(package_path, functions.__name__ + "."):
            modules.append(__import__(item.name, fromlist=["*"]))
    seen: set[type] = set()
    for module in modules:
        for value in vars(module).values():
            if not inspect.isclass(value) or value in seen:
                continue
            try:
                is_request = issubclass(value, tl_request) and value is not tl_request
            except TypeError:
                is_request = False
            if is_request and value.__module__ == module.__name__:
                seen.add(value)
                yield value


def _canonical_name(request_class: type) -> tuple[str, str]:
    module = request_class.__module__
    marker = ".functions"
    suffix = module.split(marker, 1)[1].lstrip(".") if marker in module else ""
    namespace = suffix or "core"
    prefix = "functions" + (f".{suffix}" if suffix else "")
    return f"{prefix}.{request_class.__name__}", namespace


def _verb(name: str) -> str:
    return name.rsplit(".", 1)[-1].removesuffix("Request")


def _word_tokens(value: str) -> set[str]:
    pieces = re.split(r"[^a-zA-Z0-9]+", value)
    result: set[str] = set()
    for piece in pieces:
        result.update(part.lower() for part in _CAMEL.split(piece) if part)
    return result


def classify_request(name: str) -> tuple[str, str, str]:
    """Return ``(scope, risk, reason)`` from the exact installed constructor name.

    This is intentionally conservative.  Classification grants no authority: raw
    dispatch remains sovereign-only regardless of the returned scope.
    """

    parts = name.split(".")
    namespace = parts[1] if len(parts) > 2 else "core"
    verb = _verb(name)
    lower = verb.lower()
    tokens = _word_tokens(verb)

    if namespace == "core" and verb.lower() in {"destroyauthkey", "destroysession"}:
        return (
            "telegram.account",
            "account_critical",
            "destroying an auth key/session requires separate owner confirmation",
        )
    if namespace == "core":
        return (
            "telegram.account",
            "transport_internal",
            "transport/envelope TL constructors are discoverable but never raw-dispatchable",
        )

    membership_verbs = {
        "joinchannel",
        "leavechannel",
        "importchatinvite",
        "joinchatlistinvite",
        "joinchatlistupdates",
        "leavechatlist",
        "createchat",
        "createchannel",
        "migratechat",
    }
    moderation_fragments = (
        "editadmin",
        "editbanned",
        "bannedrights",
        "promote",
        "demote",
        "ban",
        "unban",
        "adminlog",
        "hidechatjoinrequest",
        "hideallchatjoinrequests",
        "toggleantispam",
        "toggleslowmode",
        "participantshidden",
        "deleteparticipanthistory",
        "deleteparticipantreaction",
        "addchatuser",
        "deletechatuser",
        "invitetochannel",
    )
    communicate_fragments = (
        "send",
        "forward",
        "editmessage",
        "deletemessage",
        "reaction",
        "pollanswer",
        "sendvote",
        "typing",
        "savedraft",
        "uploadmedia",
        "sendmedia",
        "sendmultimedia",
        "scheduledmessage",
        "pinnedmessage",
        "unpin",
        "forumtopic",
        "story",
    )
    read_prefixes = (
        "get",
        "search",
        "resolve",
        "check",
        "read",
        "exportmessage",
    )

    if lower in membership_verbs:
        scope = "telegram.membership"
    elif any(fragment in lower for fragment in moderation_fragments):
        scope = "telegram.moderate"
    elif namespace == "contacts":
        scope = "telegram.read" if lower.startswith(read_prefixes) else "telegram.account"
    elif namespace in {"account", "auth", "payments", "premium", "photos", "folders", "chatlists", "smsjobs"}:
        scope = "telegram.account"
    elif namespace in {"help", "langpack", "stats", "updates", "users"}:
        scope = "telegram.read" if lower.startswith(read_prefixes) else "telegram.account"
    elif namespace == "upload":
        scope = "telegram.communicate"
    elif namespace == "phone":
        scope = "telegram.read" if lower.startswith(read_prefixes) else "telegram.communicate"
    elif lower.startswith(read_prefixes):
        scope = "telegram.read"
    elif any(fragment in lower for fragment in communicate_fragments):
        scope = "telegram.communicate"
    elif namespace in {"channels", "messages"}:
        scope = "telegram.moderate"
    elif namespace in {"stories", "bots"}:
        scope = "telegram.communicate"
    else:
        scope = "telegram.account"

    critical_fragments = (
        "password",
        "authorization",
        "passkey",
        "session",
        "authkey",
        "login",
        "logout",
        "signin",
        "signup",
        "deleteaccount",
        "changephone",
        "confirmphone",
        "verifyphone",
        "securevalue",
        "takeout",
        "tmpassword",
        "signincode",
    )
    destructive_account = scope == "telegram.account" and (
        lower.startswith(("delete", "reset", "unregister"))
        or namespace in {"payments", "premium"} and not lower.startswith(read_prefixes)
    )
    if namespace == "auth" or any(fragment in lower for fragment in critical_fragments) or destructive_account:
        return (
            scope,
            "account_critical",
            "auth/session/2FA or destructive account state requires separate owner confirmation",
        )
    return scope, "standard", "scope-classified installed TL constructor"


def _annotation_name(annotation: Any) -> str:
    if annotation is inspect.Signature.empty:
        return "any"
    if isinstance(annotation, typing.ForwardRef):
        return annotation.__forward_arg__
    if isinstance(annotation, str):
        return annotation.strip("'\"")
    origin = typing.get_origin(annotation)
    if origin in (list, typing.List):
        args = typing.get_args(annotation)
        return f"list[{_annotation_name(args[0]) if args else 'any'}]"
    if origin in (typing.Union, pytypes.UnionType):
        return " | ".join(_annotation_name(arg) for arg in typing.get_args(annotation))
    if annotation is type(None):
        return "null"
    return getattr(annotation, "__name__", str(annotation).replace("typing.", ""))


def _annotation_atoms(annotation: Any) -> list[Any]:
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, pytypes.UnionType):
        atoms: list[Any] = []
        for item in typing.get_args(annotation):
            atoms.extend(_annotation_atoms(item))
        return atoms
    return [annotation]


def _is_entity_annotation(annotation: Any) -> bool:
    for atom in _annotation_atoms(annotation):
        name = _annotation_name(atom)
        if name in _ENTITY_TYPES or name.startswith(("TypeInputPeer", "TypeInputChannel", "TypeInputUser")):
            return True
    return False


def _annotation_schema(annotation: Any, *, entity: bool = False) -> dict[str, Any]:
    if entity:
        return {
            "description": f"entity reference resolved as {_annotation_name(annotation)}",
            "oneOf": [
                {"type": "integer"},
                {"type": "string"},
                {"type": "object", "required": ["_"], "additionalProperties": True},
            ],
        }
    if annotation is inspect.Signature.empty:
        return {}
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, pytypes.UnionType):
        return {"anyOf": [_annotation_schema(arg) for arg in typing.get_args(annotation)]}
    if origin in (list, typing.List):
        args = typing.get_args(annotation)
        return {"type": "array", "items": _annotation_schema(args[0]) if args else {}}
    if annotation is type(None):
        return {"type": "null"}
    if annotation is bool:
        return {"type": "boolean"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    if annotation is str:
        return {"type": "string"}
    if annotation is bytes:
        return {
            "type": "object",
            "required": ["$bytes_base64"],
            "properties": {
                "$bytes_base64": {"type": "string"},
                "size": {"type": "integer", "minimum": 0},
            },
            "additionalProperties": False,
        }
    if annotation in (dt.datetime, dt.date):
        return {"type": "string", "format": "date-time" if annotation is dt.datetime else "date"}
    return {
        "type": "object",
        "required": ["_"],
        "description": _annotation_name(annotation),
        "additionalProperties": True,
    }


def _parameter_descriptors(request_class: type) -> tuple[ParameterDescriptor, ...]:
    result: list[ParameterDescriptor] = []
    for parameter in inspect.signature(request_class).parameters.values():
        required = parameter.default is inspect.Signature.empty
        default = _MISSING if required else parameter.default
        result.append(
            ParameterDescriptor(
                name=parameter.name,
                type_name=_annotation_name(parameter.annotation),
                required=required,
                default=default,
                entity=_is_entity_annotation(parameter.annotation),
                annotation=parameter.annotation,
            )
        )
    return tuple(result)


class TelegramCapabilityRegistry:
    """Immutable view over the request constructors shipped by installed Telethon."""

    def __init__(self) -> None:
        functions, tl_types, tl_object, tl_request, version, layer = _load_telethon()
        self._functions = functions
        self._types = tl_types
        self._tl_object = tl_object
        self._tl_request = tl_request
        self.telethon_version = version
        self.tl_layer = layer
        descriptors: list[RequestDescriptor] = []
        for request_class in _iter_request_classes(functions, tl_request):
            name, namespace = _canonical_name(request_class)
            scope, risk, reason = classify_request(name)
            descriptors.append(
                RequestDescriptor(
                    name=name,
                    namespace=namespace,
                    request_class=request_class,
                    constructor_id=int(getattr(request_class, "CONSTRUCTOR_ID")),
                    result_type_id=getattr(request_class, "SUBCLASS_OF_ID", None),
                    scope=scope,
                    risk=risk,
                    policy_reason=reason,
                    parameters=_parameter_descriptors(request_class),
                )
            )
        descriptors.sort(key=lambda item: item.name.lower())
        self._items = tuple(descriptors)
        self._by_name = {descriptor.name: descriptor for descriptor in descriptors}
        self._aliases: dict[str, set[str]] = {}
        for descriptor in descriptors:
            aliases = {
                descriptor.name,
                descriptor.name.removeprefix("functions."),
                descriptor.short_name,
                "telethon.tl." + descriptor.name,
            }
            for alias in aliases:
                self._aliases.setdefault(alias.lower(), set()).add(descriptor.name)
        fingerprint_rows = [
            (item.name, item.constructor_id, [(p.name, p.type_name, p.required) for p in item.parameters])
            for item in descriptors
        ]
        self.fingerprint = hashlib.sha256(
            json.dumps(fingerprint_rows, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        ).hexdigest()

    def __len__(self) -> int:
        return len(self._items)

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "telethon_version": self.telethon_version,
            "tl_layer": self.tl_layer,
            "request_count": len(self),
            "fingerprint": self.fingerprint,
            "scopes": list(SCOPES),
        }

    def get(self, name: str) -> RequestDescriptor:
        candidates = self._aliases.get(str(name).strip().lower(), set())
        if not candidates:
            raise RegistryLookupError(f"unknown Telethon request: {name}")
        if len(candidates) > 1:
            raise RegistryLookupError(
                f"ambiguous Telethon request {name!r}; use one of: {', '.join(sorted(candidates))}"
            )
        return self._by_name[next(iter(candidates))]

    def list(
        self,
        *,
        scope: str | None = None,
        namespace: str | None = None,
        risk: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        if scope is not None and scope not in SCOPES:
            raise ValueError(f"unknown Telegram scope: {scope}")
        if offset < 0:
            raise ValueError("offset must be >= 0")
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        items = [
            item
            for item in self._items
            if (scope is None or item.scope == scope)
            and (namespace is None or item.namespace == namespace)
            and (risk is None or item.risk == risk)
        ]
        page = items[offset : offset + limit]
        return {
            **self.metadata,
            "total": len(items),
            "offset": offset,
            "limit": limit,
            "items": [item.to_dict(detailed=False) for item in page],
        }

    def search(
        self,
        query: str,
        *,
        scope: str | None = None,
        limit: int = 25,
    ) -> dict[str, Any]:
        if scope is not None and scope not in SCOPES:
            raise ValueError(f"unknown Telegram scope: {scope}")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        query = str(query or "").strip().lower()
        query_tokens = _word_tokens(query)
        query_compact = re.sub(r"[^a-z0-9]", "", query)
        ranked: list[tuple[int, str, RequestDescriptor]] = []
        for item in self._items:
            if scope is not None and item.scope != scope:
                continue
            haystack = item.name.lower()
            short = item.short_name.lower()
            tokens = _word_tokens(item.name)
            if query and query not in haystack and not query_tokens.issubset(tokens):
                continue
            score = 0
            if query == item.name.lower():
                score += 1000
            if query == short or query == short.removesuffix("request"):
                score += 800
            short_compact = re.sub(r"[^a-z0-9]", "", short.removesuffix("request"))
            if query_compact and query_compact == short_compact:
                score += 700
            if short.startswith(query):
                score += 300
            if query in short:
                score += 200
            score += len(query_tokens & tokens) * 25
            ranked.append((-score, item.name.lower(), item))
        ranked.sort()
        matches = [row[2] for row in ranked[:limit]]
        return {
            **self.metadata,
            "query": query,
            "total": len(ranked),
            "items": [item.to_dict(detailed=False) for item in matches],
        }

    def describe(self, name: str) -> dict[str, Any]:
        return {**self.metadata, "request": self.get(name).to_dict(detailed=True)}

    def validate(self, name: str, parameters: Mapping[str, Any] | None) -> dict[str, Any]:
        descriptor = self.get(name)
        if parameters is None:
            parameters = {}
        if not isinstance(parameters, Mapping):
            raise ParameterValidationError("parameters must be an object")
        fields = {parameter.name: parameter for parameter in descriptor.parameters}
        unknown = sorted(set(parameters) - set(fields))
        if unknown:
            raise ParameterValidationError(
                f"{descriptor.name}: unknown parameters: {', '.join(unknown)}"
            )
        missing = [parameter.name for parameter in descriptor.parameters if parameter.required and parameter.name not in parameters]
        if missing:
            raise ParameterValidationError(
                f"{descriptor.name}: missing required parameters: {', '.join(missing)}"
            )
        result: dict[str, Any] = {}
        for name_, value in parameters.items():
            parameter = fields[name_]
            try:
                result[name_] = self._coerce_value(
                    value,
                    parameter.annotation,
                    path=f"{descriptor.name}.{name_}",
                    allow_none=parameter.default is None,
                )
            except ParameterValidationError:
                raise
            except Exception as exc:
                raise ParameterValidationError(f"{descriptor.name}.{name_}: {exc}") from exc
        return result

    def parameters_commitment(
        self, name: str, parameters: Mapping[str, Any] | None
    ) -> str:
        """Return a process-local keyed commitment, never a plain secret verifier."""

        validated = self.validate(name, parameters)
        return _json_commitment(validated)

    def _coerce_value(self, value: Any, annotation: Any, *, path: str, allow_none: bool = False) -> Any:
        if value is None:
            atoms = _annotation_atoms(annotation)
            if allow_none or type(None) in atoms or annotation is inspect.Signature.empty:
                return None
            raise ParameterValidationError(f"{path}: null is not valid for {_annotation_name(annotation)}")
        if annotation is inspect.Signature.empty:
            return value
        origin = typing.get_origin(annotation)
        if origin in (typing.Union, pytypes.UnionType):
            failures: list[str] = []
            for branch in typing.get_args(annotation):
                if branch is type(None):
                    continue
                try:
                    return self._coerce_value(value, branch, path=path)
                except ParameterValidationError as exc:
                    failures.append(str(exc))
            raise ParameterValidationError(
                f"{path}: value does not match {_annotation_name(annotation)}"
                + (f" ({'; '.join(failures[:2])})" if failures else "")
            )
        if origin in (list, typing.List):
            if not isinstance(value, (list, tuple)):
                raise ParameterValidationError(f"{path}: expected array")
            args = typing.get_args(annotation)
            subtype = args[0] if args else inspect.Signature.empty
            return [
                self._coerce_value(item, subtype, path=f"{path}[{index}]")
                for index, item in enumerate(value)
            ]
        if annotation is bool:
            if type(value) is not bool:
                raise ParameterValidationError(f"{path}: expected boolean")
            return value
        if annotation is int:
            if type(value) is not int:
                raise ParameterValidationError(f"{path}: expected integer")
            return value
        if annotation is float:
            if type(value) not in (int, float):
                raise ParameterValidationError(f"{path}: expected number")
            return float(value)
        if annotation is str:
            if not isinstance(value, str):
                raise ParameterValidationError(f"{path}: expected string")
            return value
        if annotation is bytes:
            if isinstance(value, bytes):
                return value
            if (
                isinstance(value, Mapping)
                and "$bytes_base64" in value
                and set(value) <= {"$bytes_base64", "size"}
            ):
                try:
                    decoded = base64.b64decode(str(value["$bytes_base64"]), validate=True)
                except Exception as exc:
                    raise ParameterValidationError(f"{path}: invalid base64 bytes") from exc
                if "size" in value and value["size"] != len(decoded):
                    raise ParameterValidationError(f"{path}: byte size does not match base64 payload")
                return decoded
            raise ParameterValidationError(f"{path}: expected bytes or {{$bytes_base64: ...}}")
        if annotation in (dt.datetime, dt.date):
            if isinstance(value, annotation):
                return value
            if isinstance(value, str):
                try:
                    normalized = value.replace("Z", "+00:00")
                    return annotation.fromisoformat(normalized)
                except ValueError as exc:
                    raise ParameterValidationError(f"{path}: invalid ISO date/time") from exc
            raise ParameterValidationError(f"{path}: expected ISO date/time")

        expected_name = _annotation_name(annotation)
        if isinstance(value, self._tl_object):
            self._check_tl_compatibility(value, expected_name, path)
            return value
        if isinstance(value, Mapping) and "_" in value:
            obj = self._build_tl_object(value, path=path)
            self._check_tl_compatibility(obj, expected_name, path)
            return obj
        if _is_entity_annotation(annotation) and isinstance(value, (str, int)) and type(value) is not bool:
            # Resolution is deliberately deferred to the injected entity resolver.
            return value
        raise ParameterValidationError(
            f"{path}: expected {_annotation_name(annotation)} TL object"
            + (" or entity reference" if _is_entity_annotation(annotation) else "")
        )

    def _build_tl_object(self, value: Mapping[str, Any], *, path: str) -> Any:
        raw_name = str(value.get("_", "")).strip()
        type_name = raw_name.removeprefix("telethon.tl.").removeprefix("types.")
        if "." in type_name:
            raise ParameterValidationError(f"{path}: nested TL type must be types.*")
        type_class = getattr(self._types, type_name, None)
        if not inspect.isclass(type_class):
            raise ParameterValidationError(f"{path}: unknown installed TL type {raw_name!r}")
        try:
            if not issubclass(type_class, self._tl_object) or issubclass(type_class, self._tl_request):
                raise ParameterValidationError(f"{path}: {raw_name!r} is not a data TL type")
        except TypeError as exc:
            raise ParameterValidationError(f"{path}: invalid TL type {raw_name!r}") from exc
        signature = inspect.signature(type_class)
        provided = {key: item for key, item in value.items() if key != "_"}
        unknown = sorted(set(provided) - set(signature.parameters))
        if unknown:
            raise ParameterValidationError(f"{path}: unknown TL fields: {', '.join(unknown)}")
        missing = [
            parameter.name
            for parameter in signature.parameters.values()
            if parameter.default is inspect.Signature.empty and parameter.name not in provided
        ]
        if missing:
            raise ParameterValidationError(f"{path}: missing TL fields: {', '.join(missing)}")
        kwargs: dict[str, Any] = {}
        for key, item in provided.items():
            parameter = signature.parameters[key]
            kwargs[key] = self._coerce_value(
                item,
                parameter.annotation,
                path=f"{path}.{key}",
                allow_none=parameter.default is None,
            )
        try:
            return type_class(**kwargs)
        except (TypeError, ValueError) as exc:
            raise ParameterValidationError(f"{path}: cannot build {type_name}: {exc}") from exc

    def _check_tl_compatibility(self, value: Any, expected_name: str, path: str) -> None:
        expected = getattr(self._types, expected_name, None)
        if expected is None:
            return
        candidates = typing.get_args(expected) if typing.get_origin(expected) in (typing.Union, pytypes.UnionType) else (expected,)
        classes = tuple(candidate for candidate in candidates if inspect.isclass(candidate))
        if classes and not isinstance(value, classes):
            raise ParameterValidationError(
                f"{path}: {value.__class__.__name__} does not match {expected_name}"
            )


def to_jsonable(value: Any, *, _depth: int = 0) -> Any:
    """Lossless JSON-safe serialization for TL requests, results and receipts."""

    if _depth > 40:
        raise ValueError("TL value nesting exceeds 40 levels")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return {
            "$bytes_base64": base64.b64encode(value).decode("ascii"),
            "size": len(value),
        }
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {
            str(key): to_jsonable(item, _depth=_depth + 1)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_jsonable(item, _depth=_depth + 1) for item in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return to_jsonable(to_dict(), _depth=_depth + 1)
    if hasattr(value, "__dict__"):
        return {
            "_": value.__class__.__name__,
            **{
                str(key): to_jsonable(item, _depth=_depth + 1)
                for key, item in vars(value).items()
                if not str(key).startswith("_")
            },
        }
    return repr(value)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        to_jsonable(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _json_digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _json_commitment(value: Any) -> str:
    return hmac.new(
        _PARAMETER_COMMITMENT_KEY, _canonical_json(value), hashlib.sha256,
    ).hexdigest()


async def _await_if_needed(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _error_payload(exc: BaseException) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": exc.__class__.__name__,
        "module": exc.__class__.__module__,
        "message": str(exc),
    }
    for name in ("code", "seconds", "new_dc", "request", "capture"):
        if hasattr(exc, name):
            payload[name] = to_jsonable(getattr(exc, name))
    return payload


def _retry_payload(exc: BaseException) -> dict[str, Any] | None:
    name = exc.__class__.__name__.lower()
    if "floodwait" in name or "slowmodewait" in name:
        return {
            "state": "flood_wait",
            "retry_after_seconds": getattr(exc, "seconds", None),
            "sleep_performed": False,
        }
    if "migrate" in name:
        return {
            "state": "dc_migration",
            "new_dc": getattr(exc, "new_dc", None),
            "sleep_performed": False,
        }
    if "filereference" in name and ("expired" in name or "empty" in name):
        return {"state": "file_reference_expired", "sleep_performed": False}
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)) or any(
        token in name for token in ("connection", "disconnect", "timeout")
    ):
        return {"state": "reconnect", "sleep_performed": False}
    return None


def _collect_identifiers(value: Any) -> dict[str, list[int]]:
    peer_ids: set[int] = set()
    message_ids: set[int] = set()

    def visit(item: Any, key: str = "", depth: int = 0) -> None:
        if depth > 30:
            return
        if isinstance(item, Mapping):
            for child_key, child in item.items():
                visit(child, str(child_key), depth + 1)
            return
        if isinstance(item, (list, tuple)):
            for child in item:
                visit(child, key, depth + 1)
            return
        if type(item) is int:
            lowered = key.lower()
            if lowered in {"peer_id", "chat_id", "channel_id", "user_id"}:
                peer_ids.add(item)
            elif lowered in {"message_id", "msg_id", "top_msg_id", "reply_to_msg_id"}:
                message_ids.add(item)

    visit(to_jsonable(value))
    return {"peer_ids": sorted(peer_ids), "message_ids": sorted(message_ids)}


class TelegramAccountDispatcher:
    """Policy-aware dispatcher over an injected Telethon-compatible async caller.

    ``typed_allowlist`` contains exact constructors that a future high-level typed
    surface has intentionally mapped.  Passing ``mode='typed'`` does not by itself
    downgrade an arbitrary raw request.  The default allowlist is empty.
    """

    def __init__(
        self,
        *,
        caller: AsyncRequestCaller,
        entity_resolver: EntityResolver | None,
        owner_id: str | int,
        registry: TelegramCapabilityRegistry | None = None,
        confirmation_verifier: Callable[
            [CriticalConfirmation, ConfirmationBinding], bool | Awaitable[bool]
        ]
        | None = None,
        typed_allowlist: Iterable[str] = (),
        sovereign_principals: Iterable[str] = (),
    ) -> None:
        if not callable(caller):
            raise TypeError("caller must be callable")
        self.registry = registry or TelegramCapabilityRegistry()
        self.caller = caller
        self.entity_resolver = entity_resolver
        self.owner_id = str(owner_id).strip()
        self.sovereign_principals = frozenset(
            value for value in {
                self.owner_id,
                *(str(item).strip() for item in sovereign_principals),
            } if value
        )
        self.confirmation_verifier = confirmation_verifier
        self.typed_allowlist = frozenset(
            self.registry.get(name).name for name in typed_allowlist
        )

    def confirmation_binding(
        self, name: str, parameters: Mapping[str, Any] | None, *, principal: str | int
    ) -> ConfirmationBinding:
        descriptor = self.registry.get(name)
        commitment = self.registry.parameters_commitment(descriptor.name, parameters)
        return ConfirmationBinding(
            request_name=descriptor.name,
            parameter_commitment=commitment,
            principal=str(principal),
            scope=descriptor.scope,
        )

    async def dispatch(
        self,
        name: str,
        parameters: Mapping[str, Any] | None,
        *,
        principal: str | int,
        granted_scopes: Iterable[str] = (),
        mode: str = "raw",
        confirmation: CriticalConfirmation | None = None,
        delivery_context: Any = None,
    ) -> DispatchReceipt:
        started_wall = _utc_now()
        started_mono = time.monotonic()
        receipt_id = str(uuid.uuid4())
        principal_text = str(principal)
        submitted = to_jsonable(parameters or {})
        descriptor: RequestDescriptor | None = None
        commitment: str | None = None
        confirmation_policy: dict[str, Any] = {"verified": False}

        def finish(
            status: str,
            *,
            error: Mapping[str, Any] | None = None,
            retry: Mapping[str, Any] | None = None,
            serialized: Any = None,
            result: Any = None,
        ) -> DispatchReceipt:
            result_json = to_jsonable(result) if result is not None else None
            critical = bool(descriptor is not None and descriptor.requires_confirmation)
            # Account-critical constructors can carry passwords, login codes,
            # auth tokens and equally sensitive result objects.  A receipt is a
            # model/durable surface, so retain only status/provenance plus the
            # process-keyed commitment.  Unknown requests are also not echoed.
            receipt_submitted = submitted if descriptor is not None and not critical else None
            receipt_serialized = (
                to_jsonable(serialized)
                if serialized is not None and not critical else None
            )
            receipt_result = result_json if not critical else None
            receipt_error = to_jsonable(error) if error else None
            if critical and receipt_error:
                error_type = str(receipt_error.get("type") or "")
                receipt_error = {
                    "type": (
                        error_type
                        if (_SAFE_ERROR_TYPE_RE.fullmatch(error_type)
                            or error_type in _SAFE_CRITICAL_POLICY_TYPES)
                        else "CriticalDispatchError"
                    )
                }
            return DispatchReceipt(
                receipt_id=receipt_id,
                status=status,
                request_name=descriptor.name if descriptor else str(name),
                scope=descriptor.scope if descriptor else None,
                mode=mode,
                principal=principal_text,
                started_at=started_wall,
                finished_at=_utc_now(),
                duration_ms=max(0, int((time.monotonic() - started_mono) * 1000)),
                constructor_id=descriptor.constructor_id if descriptor else None,
                submitted_parameters=receipt_submitted,
                serialized_parameters=receipt_serialized,
                parameter_commitment=commitment,
                result=receipt_result,
                result_sha256=(
                    _json_digest(result_json)
                    if result_json is not None and not critical else None
                ),
                identifiers=(
                    _collect_identifiers({"parameters": serialized, "result": result_json})
                    if not critical else {}
                ),
                error=receipt_error,
                retry=to_jsonable(retry) if retry else None,
                policy={
                    "raw_sovereign_only": True,
                    "owner": principal_text == self.owner_id,
                    "sovereign": principal_text in self.sovereign_principals,
                    "confirmation": confirmation_policy,
                    "risk": descriptor.risk if descriptor else None,
                },
                delivery_context=to_jsonable(delivery_context),
            )

        try:
            descriptor = self.registry.get(name)
            validated = self.registry.validate(descriptor.name, parameters)
            commitment = _json_commitment(validated)
        except (RegistryLookupError, ParameterValidationError, ValueError) as exc:
            return finish("denied", error={"type": exc.__class__.__name__, "message": str(exc)})

        if mode not in {"raw", "typed"}:
            return finish("denied", error={"type": "PolicyError", "message": "mode must be raw or typed"})
        if mode == "raw" and str(name).strip() != descriptor.name:
            return finish(
                "denied",
                error={
                    "type": "PolicyError",
                    "message": f"raw call requires exact constructor name {descriptor.name}",
                },
            )
        if not descriptor.callable:
            return finish(
                "denied",
                error={"type": "PolicyError", "message": descriptor.policy_reason},
            )

        is_owner = principal_text == self.owner_id
        is_sovereign = principal_text in self.sovereign_principals
        grants = {str(scope) for scope in granted_scopes}
        if mode == "raw" and not is_sovereign:
            return finish(
                "denied",
                error={
                    "type": "PermissionDenied",
                    "message": "raw MTProto calls require the owner or Praxis herself",
                },
            )
        if mode == "typed" and descriptor.name not in self.typed_allowlist:
            return finish(
                "denied",
                error={
                    "type": "PermissionDenied",
                    "message": "constructor has no registered high-level typed operation",
                },
            )
        if descriptor.scope in {"telegram.membership", "telegram.account"} and not is_sovereign:
            return finish(
                "denied",
                error={
                    "type": "PermissionDenied",
                    "message": (
                        f"{descriptor.scope} account-state changes require the owner or Praxis herself"
                    ),
                },
            )
        if not is_sovereign and descriptor.scope not in grants:
            return finish(
                "denied",
                error={
                    "type": "PermissionDenied",
                    "message": f"missing capability {descriptor.scope}",
                },
            )

        if descriptor.requires_confirmation:
            binding = ConfirmationBinding(
                descriptor.name, commitment, principal_text, descriptor.scope
            )
            if confirmation is None or self.confirmation_verifier is None:
                return finish(
                    "denied",
                    error={
                        "type": "ConfirmationRequired",
                        "message": "account-critical request requires separate owner confirmation",
                        "binding": binding.to_dict(),
                    },
                )
            bound = bool(confirmation.token)
            verified = False
            if bound:
                try:
                    verified = bool(
                        await _await_if_needed(
                            self.confirmation_verifier(confirmation, binding)
                        )
                    )
                except Exception:
                    verified = False
            confirmation_policy = {
                "verified": verified,
                "bound": bound,
            }
            if not verified:
                return finish(
                    "denied",
                    error={
                        "type": "ConfirmationRejected",
                        "message": "separate confirmation was absent, mismatched, expired or consumed",
                        "binding": binding.to_dict(),
                    },
                )

        try:
            resolved = await self._resolve_parameters(descriptor, validated)
            request = descriptor.request_class(**resolved)
            serialized = request.to_dict()
        except Exception as exc:
            return finish("denied", error=_error_payload(exc))

        try:
            result = await _await_if_needed(self.caller(request))
            return finish("ok", serialized=serialized, result=result)
        except Exception as exc:
            retry = _retry_payload(exc)
            return finish(
                "retryable_error" if retry else "error",
                serialized=serialized,
                error=_error_payload(exc),
                retry=retry,
            )

    async def handle(
        self,
        action: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        principal: str | int,
        granted_scopes: Iterable[str] = (),
        confirmation: CriticalConfirmation | None = None,
        delivery_context: Any = None,
    ) -> dict[str, Any]:
        """Implement the compact ``telegram_account`` list/search/describe/call surface."""

        arguments = dict(arguments or {})
        action = str(action or "").strip().lower()
        if action == "list":
            allowed = {"scope", "namespace", "risk", "offset", "limit"}
            unknown = sorted(set(arguments) - allowed)
            if unknown:
                raise ParameterValidationError(
                    f"telegram_account.list: unknown arguments: {', '.join(unknown)}"
                )
            return {"action": action, **self.registry.list(**arguments)}
        if action == "search":
            allowed = {"query", "scope", "limit"}
            unknown = sorted(set(arguments) - allowed)
            if unknown:
                raise ParameterValidationError(
                    f"telegram_account.search: unknown arguments: {', '.join(unknown)}"
                )
            if "query" not in arguments:
                raise ParameterValidationError("telegram_account.search: query is required")
            return {"action": action, **self.registry.search(**arguments)}
        if action == "describe":
            if set(arguments) != {"name"}:
                raise ParameterValidationError(
                    "telegram_account.describe accepts exactly one argument: name"
                )
            return {"action": action, **self.registry.describe(str(arguments["name"]))}
        if action == "call":
            allowed = {"name", "parameters", "mode"}
            unknown = sorted(set(arguments) - allowed)
            if unknown:
                raise ParameterValidationError(
                    f"telegram_account.call: unknown arguments: {', '.join(unknown)}"
                )
            if "name" not in arguments:
                raise ParameterValidationError("telegram_account.call: name is required")
            receipt = await self.dispatch(
                str(arguments["name"]),
                arguments.get("parameters"),
                principal=principal,
                granted_scopes=granted_scopes,
                mode=str(arguments.get("mode", "raw")),
                confirmation=confirmation,
                delivery_context=delivery_context,
            )
            return {"action": action, "receipt": receipt.to_dict()}
        raise ParameterValidationError(
            "telegram_account.action must be list, search, describe or call"
        )

    async def _resolve_parameters(
        self, descriptor: RequestDescriptor, parameters: Mapping[str, Any]
    ) -> dict[str, Any]:
        fields = {parameter.name: parameter for parameter in descriptor.parameters}
        result: dict[str, Any] = {}
        for name, value in parameters.items():
            result[name] = await self._resolve_value(
                value,
                fields[name].annotation,
                field=name,
                request_name=descriptor.name,
            )
        return result

    async def _resolve_value(
        self,
        value: Any,
        annotation: Any,
        *,
        field: str,
        request_name: str,
    ) -> Any:
        if value is None:
            return None
        origin = typing.get_origin(annotation)
        if origin in (typing.Union, pytypes.UnionType):
            for branch in typing.get_args(annotation):
                if branch is type(None):
                    continue
                try:
                    return await self._resolve_value(
                        value, branch, field=field, request_name=request_name
                    )
                except ParameterValidationError:
                    continue
            return value
        if origin in (list, typing.List):
            args = typing.get_args(annotation)
            subtype = args[0] if args else inspect.Signature.empty
            return [
                await self._resolve_value(
                    item, subtype, field=f"{field}[{index}]", request_name=request_name
                )
                for index, item in enumerate(value)
            ]
        if isinstance(value, self.registry._tl_object):
            signature = inspect.signature(value.__class__)
            kwargs: dict[str, Any] = {}
            changed = False
            for parameter in signature.parameters.values():
                current = getattr(value, parameter.name)
                resolved = await self._resolve_value(
                    current,
                    parameter.annotation,
                    field=f"{field}.{parameter.name}",
                    request_name=request_name,
                )
                kwargs[parameter.name] = resolved
                changed = changed or resolved is not current
            if changed:
                return value.__class__(**kwargs)
            return value
        if not _is_entity_annotation(annotation) or isinstance(value, self.registry._tl_object):
            return value
        if self.entity_resolver is None:
            raise ParameterValidationError(
                f"{request_name}.{field}: scalar entity needs injected entity_resolver"
            )
        expected = _annotation_name(annotation)
        resolved = await _await_if_needed(
            self.entity_resolver(value, expected, field, request_name)
        )
        if not isinstance(resolved, self.registry._tl_object):
            raise ParameterValidationError(
                f"{request_name}.{field}: entity_resolver returned non-TLObject"
            )
        self.registry._check_tl_compatibility(
            resolved, expected, f"{request_name}.{field}"
        )
        return resolved


__all__ = [
    "SCOPES",
    "ConfirmationBinding",
    "CriticalConfirmation",
    "DispatchReceipt",
    "ParameterDescriptor",
    "ParameterValidationError",
    "RegistryLookupError",
    "RequestDescriptor",
    "TelegramAccountDispatcher",
    "TelegramCapabilityRegistry",
    "TelethonSchemaUnavailable",
    "classify_request",
    "to_jsonable",
]
