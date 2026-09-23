"""Model Context Protocol server for Resume Matcher.

Tools reach the application through an in-process ASGI bridge, so they share
the routers' validation, budgets and side effects. See
docs/agent/features/mcp.md.
"""
