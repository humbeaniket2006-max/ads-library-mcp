"""OAuth 2.1 authorization server for claude.ai, built on the MCP SDK's auth framework.

Stateless by design: client registrations, authorization codes and tokens are HMAC-signed JWTs, so they survive
Render free-tier restarts. Only single-use tracking (best effort) lives in memory.
"""
