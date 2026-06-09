"""Runtime configuration loaded from environment variables.

In Cloud Foundry the values come from `mta.yaml` module properties.
For local development, copy `.env.example` to `.env` and run uvicorn.
"""
from __future__ import annotations

import os
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


class Settings:
    # --- AI Core (Generative AI Hub) ---
    genai_destination: str = os.getenv("GENAI_DESTINATION", "aicore-genai")
    genai_chat_deployment_id: str = os.getenv("GENAI_CHAT_DEPLOYMENT_ID", "")
    genai_chat_model: str = os.getenv("GENAI_CHAT_MODEL", "gpt-4o-mini")
    genai_embed_deployment_id: str = os.getenv("GENAI_EMBED_DEPLOYMENT_ID", "")
    genai_embed_model: str = os.getenv("GENAI_EMBED_MODEL", "text-embedding-3-small")
    ai_core_api_version: str = os.getenv("AI_CORE_API_VERSION", "2024-08-01-preview")
    ai_resource_group: str = os.getenv("AI_RESOURCE_GROUP", "default")

    # --- SAP Incentive Management ---
    tcmp_destination: str = os.getenv("TCMP_DESTINATION", "TCMP_DEST")
    # Path appended to the destination URL before the entity set. The
    # `TCMP_DEST` destination resolves only to the Datasphere host; the
    # OData service is mounted under this base path. Match the UI's
    # `tcmp` data source uri (sans the leading `tcmp/`).
    tcmp_base_path: str = os.getenv(
        "TCMP_BASE_PATH",
        "/api/v1/datasphere/consumption/relational/SYZYGYDEV",
    )

    # --- Service ---
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    dev_skip_auth: bool = os.getenv("DEV_SKIP_AUTH", "false").lower() == "true"

    # --- Local dev fallback ---
    aicore_service_key_json: str = os.getenv("AICORE_SERVICE_KEY", "")

    # --- CF runtime ---
    vcap_services: str = os.getenv("VCAP_SERVICES", "")
    port: int = int(os.getenv("PORT", "8080"))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
