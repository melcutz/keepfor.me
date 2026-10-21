# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan
"""Process-wide runtime providers holder."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from keepfor.spi import Providers

_providers: Providers | None = None


def set_providers(providers: Providers) -> None:
    """Set the process-wide providers instance."""
    global _providers
    _providers = providers.resolved()


def get_providers() -> Providers:
    """Get the process-wide providers instance, creating default if unset."""
    global _providers
    if _providers is None:
        from keepfor.spi import Providers

        _providers = Providers().resolved()
    return _providers


def reset_providers() -> None:
    """Reset the process-wide providers instance (useful for test isolation)."""
    global _providers
    _providers = None


def is_multi_tenant() -> bool:
    """True when the app runs with a hosted, multi-tenant scope provider.

    The default scope provider is single-tenant: one database, one R2
    prefix and one Vectorize namespace, so the tenant scope is optional.
    Any other provider (keepfor-cloud) is multi-tenant: the scope carries
    the tenant prefix and namespace and must be passed explicitly.

    The request-scoped twin is `keepfor.deps.is_single_tenant`.
    """
    from keepfor.defaults import SingleTenantScopeProvider

    return not isinstance(get_providers().scope, SingleTenantScopeProvider)
