# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

from keepfor.routes.api import router as api_router
from keepfor.routes.auth import router as auth_router
from keepfor.routes.settings import router as settings_router
from keepfor.routes.ui import router as ui_router

__all__ = ["api_router", "auth_router", "settings_router", "ui_router"]
