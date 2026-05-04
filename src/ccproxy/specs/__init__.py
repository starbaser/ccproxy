"""Vendored fact lists and Pydantic schemas describing claude-code behavior.

Re-exports the public surface so import sites can stay terse:

    from ccproxy.specs import BASE_BETAS, get_billing_salt
"""

from ccproxy.specs.billing_salt import get_billing_cch_seed, get_billing_salt
from ccproxy.specs.claude_code_constants import (
    BASE_BETAS,
    LONG_CONTEXT_BETAS,
)
from ccproxy.specs.claude_code_request import APIRequestParams
from ccproxy.specs.model_catalog import STATIC_MODEL_CATALOG, build_catalog

__all__ = [
    "BASE_BETAS",
    "LONG_CONTEXT_BETAS",
    "STATIC_MODEL_CATALOG",
    "APIRequestParams",
    "build_catalog",
    "get_billing_cch_seed",
    "get_billing_salt",
]
