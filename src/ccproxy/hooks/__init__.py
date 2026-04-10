"""Pipeline hooks with dependency declarations.

Each hook uses the @hook decorator to declare reads/writes dependencies.
The HookDAG uses these to compute execution order via topological sort.
"""

from ccproxy.hooks.add_beta_headers import add_beta_headers
from ccproxy.hooks.extract_session_id import extract_session_id
from ccproxy.hooks.forward_oauth import forward_oauth
from ccproxy.hooks.inject_claude_code_identity import inject_claude_code_identity
from ccproxy.hooks.inject_mcp_notifications import inject_mcp_notifications

__all__ = [
    "add_beta_headers",
    "extract_session_id",
    "forward_oauth",
    "inject_claude_code_identity",
    "inject_mcp_notifications",
]
