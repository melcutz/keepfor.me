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
