#!/usr/bin/env python3
"""Standalone executable CLI runner for Keepfor.me MCP Server"""
import sys
import os

# Add parent directory to path if run directly
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.mcp.cli import main

if __name__ == "__main__":
    main()
