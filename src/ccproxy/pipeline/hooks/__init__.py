"""Pipeline hooks with dependency declarations.

Each hook uses the @hook decorator to declare reads/writes dependencies.
The HookDAG uses these to compute execution order via topological sort.
"""

from ccproxy.pipeline.hooks.add_beta_headers import add_beta_headers
from ccproxy.pipeline.hooks.capture_headers import capture_headers
from ccproxy.pipeline.hooks.extract_session import extract_session_id
from ccproxy.pipeline.hooks.forward_apikey import forward_apikey
from ccproxy.pipeline.hooks.forward_oauth import forward_oauth
from ccproxy.pipeline.hooks.inject_identity import inject_claude_code_identity
from ccproxy.pipeline.hooks.model_router import model_router
from ccproxy.pipeline.hooks.rule_evaluator import rule_evaluator

__all__ = [
    "rule_evaluator",
    "model_router",
    "extract_session_id",
    "capture_headers",
    "forward_oauth",
    "forward_apikey",
    "add_beta_headers",
    "inject_claude_code_identity",
]
