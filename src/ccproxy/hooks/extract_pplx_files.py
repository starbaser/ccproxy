"""Extract multimodal parts from incoming OpenAI requests and upload to Perplexity.

OpenAI's chat-completions format allows ``content: [{type:'image_url', image_url:{url}}, ...]``.
Naive Phase-1 behavior in ``pplx._flatten_messages`` silently drops these
parts. This hook upgrades the flow: each non-text part is fetched (data:
URIs decoded inline; ``http(s)://...`` URLs fetched via stock httpx),
validated against the Perplexity constraints (≤30 files, ≤50MB each per
``file-uploads.md:323-329``), uploaded via the
``/rest/uploads/batch_create_upload_urls`` + S3 multipart + processing
subscription chain, then attached as S3 object URLs in
``optional_params["pplx"]["attachments"]``.

The non-text parts are stripped from ``ctx.messages`` after extraction so
``_flatten_messages`` builds a clean ``query_str``.

This hook runs in the inbound DAG after ``forward_oauth`` and before
``pplx_thread_inject``. Failures raise structured ``pplx_file_*`` errors
that surface as 4xx to the OpenAI client.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlparse
from uuid import uuid4

import httpx
from curl_cffi import CurlMime
from curl_cffi.requests import Session as CurlSession
from litellm.llms.base_llm.chat.transformation import BaseLLMException

from ccproxy.config import get_config
from ccproxy.lightllm.pplx import (
    PERPLEXITY_BROWSER_UA,
    PERPLEXITY_PROVIDER_NAME,
    PERPLEXITY_SESSION_COOKIE,
    PERPLEXITY_URL_BASE,
)
from ccproxy.pipeline.hook import hook

if TYPE_CHECKING:
    from ccproxy.pipeline.context import Context

logger = logging.getLogger(__name__)

__all__ = ["extract_pplx_files", "extract_pplx_files_guard"]


_MAX_FILES = 30
_MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB per file-uploads.md
_FETCH_TIMEOUT = 10.0
_UPLOAD_TIMEOUT = 60.0
_SUBSCRIBE_TIMEOUT = 120.0
_DEFAULT_MIMETYPE = "application/octet-stream"

_BATCH_UPLOAD_URL = (
    f"{PERPLEXITY_URL_BASE}/rest/uploads/batch_create_upload_urls"
    "?version=2.18&source=default"
)
_PROCESSING_SUBSCRIBE_URL = (
    f"{PERPLEXITY_URL_BASE}/rest/sse/attachment_processing/subscribe"
)


class _PerplexityFileError(BaseLLMException):
    """Surfaced as a 4xx structured error to the OpenAI client."""


@dataclass(frozen=True)
class _FileInfo:
    filename: str
    mimetype: str
    data: bytes
    is_image: bool


def extract_pplx_files_guard(ctx: Context) -> bool:
    """Run only when forward_oauth resolved the Perplexity sentinel."""
    assert ctx.flow is not None
    return (
        ctx.flow.metadata.get("ccproxy.oauth_provider") == PERPLEXITY_PROVIDER_NAME
    )


def _collect_parts(messages: list[Any]) -> list[tuple[int, int, dict[str, Any]]]:
    """Walk messages, yielding (msg_idx, part_idx, part) for non-text content parts."""
    found: list[tuple[int, int, dict[str, Any]]] = []
    for mi, msg in enumerate(messages):
        content = (
            msg.get("content")
            if isinstance(msg, dict)
            else getattr(msg, "content", None)
        )
        if not isinstance(content, list):
            continue
        for pi, part in enumerate(content):
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype in (None, "text"):
                continue
            found.append((mi, pi, part))
    return found


def _fetch_part(part: dict[str, Any]) -> _FileInfo | None:
    """Resolve a non-text part to bytes + mimetype + filename.

    Currently handles OpenAI ``image_url`` parts (the most common multimodal
    surface). Future part types can extend this dispatch.
    """
    ptype = part.get("type")
    if ptype != "image_url":
        logger.debug("extract_pplx_files: skipping unsupported part type %r", ptype)
        return None

    image_url = part.get("image_url")
    if isinstance(image_url, dict):
        url = image_url.get("url")
    elif isinstance(image_url, str):
        url = image_url
    else:
        return None
    if not isinstance(url, str) or not url:
        return None

    if url.startswith("data:"):
        return _decode_data_uri(url)

    if url.startswith(("http://", "https://")):
        return _fetch_url(url)

    logger.warning("extract_pplx_files: unsupported url scheme: %s", url[:30])
    return None


def _decode_data_uri(url: str) -> _FileInfo | None:
    """``data:[mime];base64,<b64>`` → ``_FileInfo``."""
    try:
        header, encoded = url.split(",", 1)
    except ValueError:
        return None
    if not header.startswith("data:"):
        return None
    meta = header[5:]
    mimetype = _DEFAULT_MIMETYPE
    is_b64 = False
    for token in meta.split(";"):
        if token == "base64":
            is_b64 = True
        elif "/" in token:
            mimetype = token
    try:
        data = base64.b64decode(encoded) if is_b64 else unquote(encoded).encode()
    except Exception:
        return None
    ext = mimetypes.guess_extension(mimetype) or ".bin"
    filename = f"image{ext}"
    return _FileInfo(
        filename=filename,
        mimetype=mimetype,
        data=data,
        is_image=mimetype.startswith("image/"),
    )


def _fetch_url(url: str) -> _FileInfo | None:
    """``http(s)://...`` URL → ``_FileInfo``. Uses stock httpx; no impersonation."""
    try:
        resp = httpx.get(url, timeout=_FETCH_TIMEOUT, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise _PerplexityFileError(
            status_code=400,
            message=f"Failed to fetch image_url {url!r}: {e}",
            headers=None,
        ) from e
    parsed = urlparse(url)
    name = parsed.path.rsplit("/", 1)[-1] or "image"
    mimetype = (
        resp.headers.get("content-type", "").split(";")[0].strip()
        or mimetypes.guess_type(name)[0]
        or _DEFAULT_MIMETYPE
    )
    if "." not in name:
        ext = mimetypes.guess_extension(mimetype) or ".bin"
        name = name + ext
    return _FileInfo(
        filename=name,
        mimetype=mimetype,
        data=resp.content,
        is_image=mimetype.startswith("image/"),
    )


def _validate(files: list[_FileInfo]) -> None:
    """Per file-uploads.md:323-329: ≤30 files, ≤50MB each, non-empty."""
    if len(files) > _MAX_FILES:
        raise _PerplexityFileError(
            status_code=400,
            message=f"Too many attachments: {len(files)}. Maximum allowed is {_MAX_FILES}.",
            headers=None,
        )
    for f in files:
        size = len(f.data)
        if size == 0:
            raise _PerplexityFileError(
                status_code=400,
                message=f"Attachment {f.filename!r} is empty.",
                headers=None,
            )
        if size > _MAX_FILE_SIZE:
            raise _PerplexityFileError(
                status_code=400,
                message=(
                    f"Attachment {f.filename!r} exceeds 50 MB limit: "
                    f"{size / (1024 * 1024):.1f} MB"
                ),
                headers=None,
            )


def _batch_create_upload_urls(files: list[_FileInfo], token: str) -> dict[str, dict[str, Any]]:
    """POST batch_create_upload_urls. Returns ``{client_uuid: result_dict}``."""
    payload_files = {
        str(uuid4()): {
            "filename": f.filename,
            "content_type": f.mimetype,
            "source": "default",
            "file_size": len(f.data),
            "force_image": f.is_image,
            "skip_parsing": False,
            "persistent_upload": False,
        }
        for f in files
    }
    headers = _api_headers(token)
    headers["Content-Type"] = "application/json"
    try:
        resp = httpx.post(
            _BATCH_UPLOAD_URL,
            headers=headers,
            json={"files": payload_files},
            timeout=_UPLOAD_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise _PerplexityFileError(
            status_code=502,
            message=f"batch_create_upload_urls failed: {e}",
            headers=None,
        ) from e

    body = resp.json()
    results = body.get("results")
    if not isinstance(results, dict):
        raise _PerplexityFileError(
            status_code=502,
            message="batch_create_upload_urls returned no results",
            headers=None,
        )
    if body.get("rate_limited"):
        raise _PerplexityFileError(
            status_code=429,
            message="Perplexity rate-limited the upload batch.",
            headers=None,
        )

    return {
        client_uuid: result
        for client_uuid, result in zip(payload_files, results.values(), strict=False)
    }


def _s3_upload(file_info: _FileInfo, result: dict[str, Any]) -> str:
    """POST multipart to ``s3_bucket_url``. Returns ``s3_object_url``."""
    bucket_url = result.get("s3_bucket_url")
    object_url = result.get("s3_object_url")
    fields = result.get("fields")
    if not isinstance(bucket_url, str) or not isinstance(object_url, str):
        raise _PerplexityFileError(
            status_code=502,
            message="upload URL response missing s3_bucket_url / s3_object_url",
            headers=None,
        )
    if not isinstance(fields, dict):
        raise _PerplexityFileError(
            status_code=502,
            message="upload URL response missing presigned fields",
            headers=None,
        )

    mime = CurlMime()
    try:
        for field_name, field_value in fields.items():
            mime.addpart(name=field_name, data=str(field_value).encode("utf-8"))
        mime.addpart(
            name="file",
            content_type=file_info.mimetype,
            filename=file_info.filename,
            data=file_info.data,
        )
        with CurlSession() as session:
            resp = session.post(bucket_url, multipart=mime, timeout=_UPLOAD_TIMEOUT)
        if resp.status_code not in (200, 201, 204):
            raise _PerplexityFileError(
                status_code=502,
                message=(
                    f"S3 upload failed for {file_info.filename!r}: "
                    f"status {resp.status_code}"
                ),
                headers=None,
            )
    finally:
        mime.close()

    return object_url


def _await_processing(file_uuids: list[str], token: str) -> None:
    """Subscribe to attachment_processing SSE and drain until close."""
    if not file_uuids:
        return
    headers = _api_headers(token)
    headers["Content-Type"] = "application/json"
    headers["Accept"] = "text/event-stream"
    headers["x-perplexity-request-reason"] = "ask-input-inner-home"
    headers["x-perplexity-request-try-number"] = "1"
    headers["sec-fetch-dest"] = "empty"
    headers["sec-fetch-mode"] = "cors"
    headers["sec-fetch-site"] = "same-origin"
    try:
        with httpx.stream(
            "POST",
            _PROCESSING_SUBSCRIBE_URL,
            headers=headers,
            json={"file_uuids": file_uuids},
            timeout=_SUBSCRIBE_TIMEOUT,
        ) as resp:
            resp.raise_for_status()
            for _ in resp.iter_bytes():
                pass
    except httpx.HTTPError:
        logger.warning(
            "extract_pplx_files: attachment_processing/subscribe failed; "
            "proceeding without waiting",
            exc_info=True,
        )


def _api_headers(token: str) -> dict[str, str]:
    return {
        "Cookie": f"{PERPLEXITY_SESSION_COOKIE}={token}",
        "User-Agent": PERPLEXITY_BROWSER_UA,
        "Origin": PERPLEXITY_URL_BASE,
        "Referer": f"{PERPLEXITY_URL_BASE}/",
        "x-app-apiclient": "default",
        "x-app-apiversion": "2.18",
    }


@hook(reads=["messages"], writes=["pplx", "messages"])
def extract_pplx_files(ctx: Context, _: dict[str, Any]) -> Context:
    """Extract → upload → attach multimodal parts. See module docstring."""
    assert ctx.flow is not None
    body = ctx._body if isinstance(ctx._body, dict) else {}
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return ctx

    parts = _collect_parts(messages)
    if not parts:
        return ctx

    token = get_config().resolve_oauth_token(PERPLEXITY_PROVIDER_NAME)
    if not token:
        logger.warning(
            "extract_pplx_files: %d multimodal parts present but no session token; dropping",
            len(parts),
        )
        _strip_parts(messages, parts)
        ctx._body = body
        return ctx

    files: list[_FileInfo] = []
    for _mi, _pi, part in parts:
        info = _fetch_part(part)
        if info is not None:
            files.append(info)

    if not files:
        _strip_parts(messages, parts)
        ctx._body = body
        return ctx

    _validate(files)

    uploads = _batch_create_upload_urls(files, token)

    object_urls: list[str] = []
    file_uuids: list[str] = []
    for file_info, (_client_uuid, result) in zip(files, uploads.items(), strict=False):
        object_url = _s3_upload(file_info, result)
        object_urls.append(object_url)
        server_uuid = result.get("file_uuid")
        if isinstance(server_uuid, str):
            file_uuids.append(server_uuid)

    _await_processing(file_uuids, token)

    pplx_extras = body.get("pplx")
    if not isinstance(pplx_extras, dict):
        pplx_extras = {}
    existing = pplx_extras.get("attachments")
    merged = list(existing) if isinstance(existing, list) else []
    merged.extend(object_urls)
    pplx_extras["attachments"] = merged
    body["pplx"] = pplx_extras

    _strip_parts(messages, parts)
    ctx._body = body

    logger.info(
        "extract_pplx_files: uploaded %d attachment(s) (%s)",
        len(object_urls),
        ", ".join(f.filename for f in files),
    )
    return ctx


def _strip_parts(messages: list[Any], parts: list[tuple[int, int, dict[str, Any]]]) -> None:
    """Remove the non-text content parts identified by ``_collect_parts``."""
    by_msg: dict[int, set[int]] = {}
    for mi, pi, _ in parts:
        by_msg.setdefault(mi, set()).add(pi)
    for mi, indices in by_msg.items():
        msg = messages[mi]
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        msg["content"] = [p for i, p in enumerate(content) if i not in indices]
