"""Load the OpenAI API key from local .env or Streamlit secrets. Never log the value."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
MISSING_KEY_MESSAGE = (
    "No OpenAI API key is configured. For local development, add OPENAI_API_KEY "
    "to a project .env file. For Streamlit Community Cloud, add OPENAI_API_KEY "
    "under App settings → Secrets. The key is never shown in this app."
)


def _streamlit_secret_key() -> Optional[str]:
    try:
        import streamlit as st
    except Exception:
        return None
    try:
        secrets = st.secrets
    except Exception:
        return None
    try:
        if "OPENAI_API_KEY" in secrets:
            value = secrets["OPENAI_API_KEY"]
            if value:
                return str(value).strip()
        openai_block = secrets.get("openai") if hasattr(secrets, "get") else None
        if openai_block:
            for name in ("api_key", "OPENAI_API_KEY"):
                value = openai_block.get(name) if hasattr(openai_block, "get") else None
                if value:
                    return str(value).strip()
    except Exception:
        return None
    return None


def get_openai_api_key() -> Optional[str]:
    """Return the configured key, or None. Missing .env / secrets.toml is not an error."""
    try:
        load_dotenv(ROOT / ".env")
    except Exception:
        pass
    env_value = os.environ.get("OPENAI_API_KEY")
    if env_value and str(env_value).strip():
        return str(env_value).strip()
    try:
        secret_value = _streamlit_secret_key()
    except Exception:
        secret_value = None
    if secret_value:
        return secret_value
    return None
