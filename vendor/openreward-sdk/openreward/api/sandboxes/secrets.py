"""Well-known secret key names and their allowed external API domains."""

from __future__ import annotations

import base64
import json
from typing import Mapping

WELL_KNOWN_SECRETS: dict[str, list[str]] = {
    "OPENAI_API_KEY": ["api.openai.com"],
    "ANTHROPIC_API_KEY": ["api.anthropic.com"],
    "GEMINI_API_KEY": ["generativelanguage.googleapis.com"],
    "TAVILY_API_KEY": ["api.tavily.com"],
    "GOOGLE_API_KEY": ["generativelanguage.googleapis.com"],
    "COHERE_API_KEY": ["api.cohere.com"],
    "MISTRAL_API_KEY": ["api.mistral.ai"],
    "GROQ_API_KEY": ["api.groq.com"],
    "TOGETHER_API_KEY": ["api.together.xyz"],
    "REPLICATE_API_TOKEN": ["api.replicate.com"],
    "HUGGINGFACE_API_KEY": ["api-inference.huggingface.co", "huggingface.co"],
    "HF_TOKEN": ["huggingface.co", "api-inference.huggingface.co"],
    "HUGGING_FACE_HUB_TOKEN": ["huggingface.co", "api-inference.huggingface.co"],
    "PERPLEXITY_API_KEY": ["api.perplexity.ai"],
    "FIREWORKS_API_KEY": ["api.fireworks.ai"],
    "DEEPSEEK_API_KEY": ["api.deepseek.com"],
    "KAGGLE_KEY": ["www.kaggle.com"],
    "KAGGLE_USERNAME": ["www.kaggle.com"],
    "KAGGLE_API_KEY": ["www.kaggle.com"],
    "E2B_API_KEY": ["api.e2b.app"],
    "MODAL_TOKEN_ID": ["api.modal.com"],
    "MODAL_TOKEN_SECRET": ["api.modal.com"],
    "DAYTONA_API_KEY": ["app.daytona.io"],
    # TODO: hack; we should deal with lower-case variants properly in future.
    "openai_api_key": ["api.openai.com"],
    "anthropic_api_key": ["api.anthropic.com"],
    "gemini_api_key": ["generativelanguage.googleapis.com"],
    "tavily_api_key": ["api.tavily.com"],
    "google_api_key": ["generativelanguage.googleapis.com"],
    "cohere_api_key": ["api.cohere.com"],
    "mistral_api_key": ["api.mistral.ai"],
    "groq_api_key": ["api.groq.com"],
    "together_api_key": ["api.together.xyz"],
    "replicate_api_token": ["api.replicate.com"],
    "huggingface_api_key": ["api-inference.huggingface.co", "huggingface.co"],
    "hf_token": ["huggingface.co", "api-inference.huggingface.co"],
    "hugging_face_hub_token": ["huggingface.co", "api-inference.huggingface.co"],
    "perplexity_api_key": ["api.perplexity.ai"],
    "fireworks_api_key": ["api.fireworks.ai"],
    "deepseek_api_key": ["api.deepseek.com"],
    "kaggle_key": ["www.kaggle.com"],
    "kaggle_username": ["www.kaggle.com"],
    "kaggle_api_key": ["www.kaggle.com"],
    "e2b_api_key": ["api.e2b.app"],
    "modal_token_id": ["api.modal.com"],
    "modal_token_secret": ["api.modal.com"],
    "daytona_api_key": ["app.daytona.io"],
    "gh_auth_token": ["github.com", "api.github.com", "uploads.github.com", "objects.githubusercontent.com", "raw.githubusercontent.com"],
}


def _internal_domain(host: str) -> str | None:
    """Derive internal cluster domain from external hostname.

    e.g. sessions.openreward.ai -> sessions.openreward.internal
    """
    parts = host.split(".")
    if len(parts) >= 3:
        return f"{parts[0]}.openreward.internal"
    return None


def augment_secrets_with_api_key(
    secrets: Mapping[str, str | tuple[str, list[str]]] | None,
    api_key: str | None,
    base_url: str | None,
    api_base_url: str | None,
) -> dict[str, str | tuple[str, list[str]]] | None:
    """Augment secrets with api_key and OPENREWARD_API_KEY entries."""
    if api_key is None:
        return dict(secrets) if secrets else None

    from urllib.parse import urlparse
    domains: list[str] = []
    for url in (base_url, api_base_url):
        if url:
            host = urlparse(url).hostname
            if host:
                domains.append(host)

    # Also allow corresponding internal cluster domains
    # e.g. sessions.openreward.ai -> sessions.openreward.internal
    internal_domains = [d for h in domains if (d := _internal_domain(h)) is not None]
    domains.extend(internal_domains)

    augmented: dict[str, str | tuple[str, list[str]]] = dict(secrets) if secrets else {}
    augmented["api_key"] = (api_key, domains)
    augmented["OPENREWARD_API_KEY"] = (api_key, domains)
    return augmented


def build_secrets_header(
    secrets: Mapping[str, str | tuple[str, list[str]]],
) -> str:
    """Transform user-provided secrets into a base64-encoded JSON header value.

    Accepts ``Mapping[str, str | tuple[str, list[str]]]``:
    - ``str`` value → looks up key name in ``WELL_KNOWN_SECRETS`` for domains;
      raises ``ValueError`` if key is not a well-known name.
    - ``tuple[str, list[str]]`` → explicit ``(value, allowed_domains)``.

    Returns base64-encoded JSON: ``{key: {value, allowed_domains}}``.
    """
    payload: dict[str, dict[str, object]] = {}

    for key, entry in secrets.items():
        if isinstance(entry, tuple):
            value, allowed_domains = entry
            payload[key] = {"value": value, "allowed_domains": allowed_domains}
        else:
            # Plain string value — resolve domains from well-known list
            if key not in WELL_KNOWN_SECRETS:
                raise ValueError(
                    f"Secret key {key!r} is not in WELL_KNOWN_SECRETS. "
                    f"Pass a tuple (value, allowed_domains) to specify domains explicitly."
                )
            payload[key] = {
                "value": entry,
                "allowed_domains": WELL_KNOWN_SECRETS[key],
            }

    return base64.b64encode(json.dumps(payload).encode()).decode()
