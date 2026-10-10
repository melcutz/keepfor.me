# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""The extension manifest references icon files; missing files mean a
default puzzle-piece toolbar icon. Pure-stdlib PNG header checks."""

import json
import os
import struct

EXT_DIR = os.path.join(os.path.dirname(__file__), "..", "browser-extension")


def _read_manifest_icons():
    with open(os.path.join(EXT_DIR, "manifest.json")) as f:
        return json.load(f)["icons"]


def test_manifest_icons_exist_and_match_size():
    """Every manifest icon exists and its pixels match the declared size."""
    for size, filename in _read_manifest_icons().items():
        path = os.path.join(EXT_DIR, filename)
        assert os.path.isfile(path), f"missing extension icon: {filename}"
        with open(path, "rb") as f:
            header = f.read(26)
        assert header[:8] == b"\x89PNG\r\n\x1a\n", f"{filename} is not a PNG"
        width, height = struct.unpack(">II", header[16:24])
        assert (width, height) == (int(size), int(size)), filename
