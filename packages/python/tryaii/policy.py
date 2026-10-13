"""
Data save: model origin and provider policies (shared/datasave/SPEC.md).

A :class:`ModelPolicy` says which companies may build the models a user
routes to and which may receive their prompts. :func:`evaluate` applies it to
a catalog's ``origins.json`` (catalog contract v1.2, full catalog only) and
returns the allowed model ids plus, per model, the OpenRouter hosts a request
may use. The Router filters candidates with it; the OpenRouter integration
pins ``provider.only`` with it.

Fail closed: an unknown model, an unmapped host or a missing ``origins.json``
never passes an active policy.

This module is pure data (no engine imports). Mirrors
``packages/node/src/policy.ts``; the fixtures in shared/datasave/fixtures/
pin both.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Union

PRC = frozenset({"CN", "HK", "MO"})
EEA = frozenset({
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE",
    "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE",
    "IS", "LI", "NO",
})
US_EU = frozenset({"US", "GB", "CH"}) | EEA

ORIGIN_PRESETS = ("no-prc-models", "no-prc-hosting", "us-eu")
PLATFORMS = ("bedrock", "azure", "vertex")
PLATFORM_HOSTS: dict[str, tuple[str, ...]] = {
    "bedrock": ("amazon-bedrock",),
    "azure": ("azure",),
    "vertex": ("google-vertex",),
}

#: Exclusion reasons, in the order they are tested (SPEC section 3).
REASONS = ("unknown", "origin", "provider", "platform", "host")

POLICY_FILE = "policy.json"

MSG_NEEDS_FULL_CATALOG = (
    "Model filters (origin, providers, platform) need the full catalog. Run: tryaii login"
)

_COUNTRY_RE = re.compile(r"^[A-Za-z]{2}$")


class PolicyError(ValueError):
    """A policy value is invalid (unknown preset, platform, provider, country)."""


class PolicyCatalogError(PolicyError):
    """A policy is active but the catalog has no origin data (starter catalog)."""

    def __init__(self, message: str = MSG_NEEDS_FULL_CATALOG):
        super().__init__(message)


def _names(value: Any, label: str) -> tuple[str, ...]:
    """A list or comma-separated string of names -> lowercased, deduplicated, sorted."""
    if value is None:
        return ()
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        raise PolicyError(f"{label} must be a list or a comma-separated string")
    out = set()
    for item in items:
        if not isinstance(item, str):
            raise PolicyError(f"{label} must contain strings")
        item = item.strip().lower()
        if item:
            out.add(item)
    return tuple(sorted(out))


def _countries(value: Any, label: str) -> tuple[str, ...]:
    out = set()
    for code in _names(value, label):
        code = code.upper()
        if not _COUNTRY_RE.match(code):
            raise PolicyError(f"{label}: {code!r} is not a two-letter country code")
        out.add(code)
    return tuple(sorted(out))


@dataclass(frozen=True)
class ModelPolicy:
    """Which model makers and hosts are allowed (SPEC section 2).

    Every field is optional; a policy with no field set is inactive. Lists
    are normalized (deduplicated, sorted; providers lowercased, countries
    uppercased) so equal policies compare and serialize equally.
    """

    origin: Optional[str] = None
    providers: tuple[str, ...] = ()
    exclude_providers: tuple[str, ...] = ()
    allow_countries: tuple[str, ...] = ()
    deny_countries: tuple[str, ...] = ()
    platform: Optional[str] = None

    def __post_init__(self) -> None:
        origin = self.origin.strip().lower() if isinstance(self.origin, str) else self.origin
        if origin == "":
            origin = None
        if origin is not None and origin not in ORIGIN_PRESETS:
            raise PolicyError(
                f"unknown origin {self.origin!r}; use one of: {', '.join(ORIGIN_PRESETS)}")
        platform = self.platform
        if isinstance(platform, str):
            platform = platform.strip().lower()
        if platform == "":
            platform = None
        if platform is not None and platform not in PLATFORMS:
            raise PolicyError(
                f"unknown platform {self.platform!r}; use one of: {', '.join(PLATFORMS)}")
        object.__setattr__(self, "origin", origin)
        object.__setattr__(self, "platform", platform)
        object.__setattr__(self, "providers", _names(self.providers, "providers"))
        object.__setattr__(self, "exclude_providers",
                           _names(self.exclude_providers, "exclude_providers"))
        object.__setattr__(self, "allow_countries",
                           _countries(self.allow_countries, "allow_countries"))
        object.__setattr__(self, "deny_countries",
                           _countries(self.deny_countries, "deny_countries"))

    # ------------------------------------------------------------ shape
    @property
    def active(self) -> bool:
        return bool(self.origin or self.providers or self.exclude_providers
                    or self.allow_countries or self.deny_countries or self.platform)

    @property
    def constrains_models(self) -> bool:
        return bool(self.origin in ("no-prc-models", "us-eu") or self.providers
                    or self.exclude_providers or self.allow_countries or self.deny_countries)

    @property
    def constrains_hosts(self) -> bool:
        return bool(self.origin in ("no-prc-hosting", "us-eu") or self.providers
                    or self.exclude_providers or self.allow_countries or self.deny_countries
                    or self.platform)

    def to_dict(self) -> dict:
        return {
            "origin": self.origin,
            "providers": list(self.providers),
            "exclude_providers": list(self.exclude_providers),
            "allow_countries": list(self.allow_countries),
            "deny_countries": list(self.deny_countries),
            "platform": self.platform,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ModelPolicy:
        if not isinstance(data, Mapping):
            raise PolicyError("a policy must be a JSON object")
        unknown = sorted(set(data) - set(_FIELDS))
        if unknown:
            raise PolicyError(f"unknown policy field(s): {', '.join(unknown)}")
        return cls(**{k: data[k] for k in _FIELDS if k in data and data[k] is not None})

    def merged(self, override: Mapping[str, Any]) -> ModelPolicy:
        """This policy with each non-empty field of ``override`` replacing ours
        (CLI flags over the saved policy, field by field)."""
        current = self.to_dict()
        for key in _FIELDS:
            value = override.get(key)
            if value not in (None, "", (), []):
                current[key] = value
        return ModelPolicy.from_dict(current)

    def summary(self) -> str:
        """The ``Policy:`` line text (SPEC section 5); "" when inactive."""
        parts = []
        if self.origin:
            parts.append(f"origin {self.origin}")
        if self.providers:
            parts.append(f"providers {','.join(self.providers)}")
        if self.exclude_providers:
            parts.append(f"exclude-providers {','.join(self.exclude_providers)}")
        if self.allow_countries:
            parts.append(f"allow-countries {','.join(self.allow_countries)}")
        if self.deny_countries:
            parts.append(f"deny-countries {','.join(self.deny_countries)}")
        if self.platform:
            parts.append(f"platform {self.platform}")
        return "; ".join(parts)


_FIELDS = ("origin", "providers", "exclude_providers", "allow_countries", "deny_countries",
           "platform")


# ---------------------------------------------------------------- evaluation
def base_model_id(model_id: str) -> str:
    """``a/b:free`` -> ``a/b`` (variants share their base model's origin entry)."""
    return model_id.split(":", 1)[0]


@dataclass
class PolicyResult:
    """:func:`evaluate`'s output."""

    policy: ModelPolicy
    #: Allowed model ids, in input order.
    allowed: list[str] = field(default_factory=list)
    #: ``{model_id: reason}`` for every excluded id (reason in ``REASONS``).
    excluded: dict[str, str] = field(default_factory=dict)
    #: ``{model_id: [host slugs]}`` for allowed ids when the host constraint
    #: is active -- the OpenRouter ``provider.only`` list.
    hosts: dict[str, list[str]] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.allowed) + len(self.excluded)

    def provider_preferences(self, model_id: str) -> Optional[dict]:
        """The OpenRouter ``provider`` object for ``model_id``, or None when no
        host constraint applies."""
        if not self.policy.constrains_hosts:
            return None
        return {"only": list(self.hosts.get(model_id, []))}


def _resolve_providers(names: tuple[str, ...], orgs: Mapping[str, Any], label: str) -> frozenset:
    """Map provider names (org slugs or owned model namespaces) to org slugs."""
    aliases: dict[str, str] = {}
    for slug, org in orgs.items():
        aliases[slug.lower()] = slug
    for slug, org in orgs.items():
        for ns in org.get("namespaces") or []:
            aliases.setdefault(str(ns).lower(), slug)
    out = set()
    unknown = []
    for name in names:
        if name in aliases:
            out.add(aliases[name])
        else:
            unknown.append(name)
    if unknown:
        raise PolicyError(
            f"unknown {label}: {', '.join(unknown)} (use company names such as "
            f"anthropic, openai, google, mistral, or a model-id namespace)")
    return frozenset(out)


def _country_ok(country: Optional[str], policy: ModelPolicy, preset_test) -> bool:
    if country is None:
        return False
    if preset_test is not None and not preset_test(country):
        return False
    if policy.allow_countries and country not in policy.allow_countries:
        return False
    if policy.deny_countries and country in policy.deny_countries:
        return False
    return True


def _model_preset(origin: Optional[str]):
    if origin == "no-prc-models":
        return lambda c: c not in PRC
    if origin == "us-eu":
        return lambda c: c in US_EU
    return None


def _host_preset(origin: Optional[str]):
    if origin == "no-prc-hosting":
        return lambda c: c not in PRC
    if origin == "us-eu":
        return lambda c: c in US_EU
    return None


def evaluate(policy: ModelPolicy, origins: Optional[Mapping[str, Any]],
             model_ids: Iterable[str]) -> PolicyResult:
    """Apply ``policy`` to ``model_ids`` using a catalog's ``origins.json``.

    An inactive policy allows everything (``origins`` may be None then). An
    active policy with ``origins`` None raises :class:`PolicyCatalogError`.
    """
    ids = list(model_ids)
    result = PolicyResult(policy=policy)
    if not policy.active:
        result.allowed = ids
        return result
    if not origins:
        raise PolicyCatalogError()

    orgs: Mapping[str, Any] = origins.get("orgs") or {}
    hosts: Mapping[str, Any] = origins.get("hosts") or {}
    entries: Mapping[str, Any] = origins.get("models") or {}

    providers = _resolve_providers(policy.providers, orgs, "provider")
    excluded_orgs = _resolve_providers(policy.exclude_providers, orgs, "provider")

    def control(org_slug: Optional[str]) -> Optional[str]:
        org = orgs.get(org_slug) if org_slug else None
        return org.get("control_country") if org else None

    model_test = _model_preset(policy.origin)
    host_test = _host_preset(policy.origin)
    platform_hosts = set(PLATFORM_HOSTS[policy.platform]) if policy.platform else None

    allowed_hosts = set()
    if policy.constrains_hosts:
        for slug, host in hosts.items():
            org = host.get("org")
            if not org or org not in orgs:
                continue
            if not _country_ok(control(org), policy, host_test):
                continue
            if providers and org not in providers:
                continue
            if org in excluded_orgs:
                continue
            if platform_hosts is not None and slug not in platform_hosts:
                continue
            allowed_hosts.add(slug)

    for model_id in ids:
        entry = entries.get(base_model_id(model_id))
        reason = None
        if policy.constrains_models:
            if not entry or not entry.get("known"):
                reason = "unknown"
            else:
                lineage = list(entry.get("lineage") or [])
                if not lineage or not all(
                        _country_ok(control(o), policy, model_test) for o in lineage):
                    reason = "origin"
                elif (providers and entry.get("developer") not in providers) or any(
                        o in excluded_orgs for o in lineage):
                    reason = "provider"
        if reason is None and policy.platform:
            if not entry or policy.platform not in (entry.get("platforms") or {}):
                reason = "platform"
        model_hosts: list[str] = []
        if reason is None and policy.constrains_hosts:
            model_hosts = sorted(h for h in (entry or {}).get("hosts") or [] if h in allowed_hosts)
            if not model_hosts:
                reason = "host"
        if reason is None:
            result.allowed.append(model_id)
            if policy.constrains_hosts:
                result.hosts[model_id] = model_hosts
        else:
            result.excluded[model_id] = reason
    return result


def origin_label(origins: Optional[Mapping[str, Any]], model_id: str) -> str:
    """Lineage control countries joined by ``+`` (``?`` when unknown) -- the
    ``models`` command's ``origin:`` column."""
    if not origins:
        return "?"
    entry = (origins.get("models") or {}).get(base_model_id(model_id))
    if not entry or not entry.get("known"):
        return "?"
    orgs = origins.get("orgs") or {}
    countries = []
    for slug in entry.get("lineage") or []:
        country = (orgs.get(slug) or {}).get("control_country")
        if not country:
            return "?"
        if country not in countries:
            countries.append(country)
    return "+".join(countries) if countries else "?"


# ---------------------------------------------------------------- saved policy
def policy_path() -> Path:
    from tryaii.auth.store import data_dir

    return data_dir() / POLICY_FILE


def load_saved_policy() -> ModelPolicy:
    """The saved policy (``<data dir>/policy.json``), or an inactive one.

    A malformed file raises :class:`PolicyError` -- a broken policy file must
    not silently turn filtering off."""
    path = policy_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ModelPolicy()
    except OSError as exc:
        raise PolicyError(f"could not read {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise PolicyError(f"{path} is not valid JSON: {exc}") from exc
    return ModelPolicy.from_dict(data)


def save_policy(policy: ModelPolicy) -> Path:
    """Write ``policy`` to ``<data dir>/policy.json`` (atomic)."""
    from tryaii.auth.store import write_atomic

    path = policy_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(policy.to_dict(), indent=2) + "\n"
    write_atomic(path, text.encode("utf-8"))
    return path


def clear_saved_policy() -> bool:
    """Delete the saved policy; True when a file was removed."""
    try:
        os.remove(policy_path())
        return True
    except FileNotFoundError:
        return False


PolicyLike = Union[ModelPolicy, Mapping[str, Any], str, None]


def resolve_policy(policy: PolicyLike = "auto") -> ModelPolicy:
    """``"auto"`` = the saved policy; None = no policy; a dict or
    :class:`ModelPolicy` = that policy."""
    if policy is None:
        return ModelPolicy()
    if isinstance(policy, ModelPolicy):
        return policy
    if isinstance(policy, str):
        if policy != "auto":
            raise PolicyError(
                f"policy must be 'auto', None, a dict or a ModelPolicy; got {policy!r}")
        return load_saved_policy()
    if isinstance(policy, Mapping):
        return ModelPolicy.from_dict(policy)
    raise PolicyError(f"policy must be 'auto', None, a dict or a ModelPolicy; got {type(policy)!r}")
