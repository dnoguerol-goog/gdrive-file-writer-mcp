# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Configuration management for Google Drive File Writer MCP server."""

import json
import logging
from typing import Any
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Server and Google Drive configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Primary Google Shared Drive folder ID
    drive_folder_id: str = Field(
        default="",
        validation_alias="DRIVE_FOLDER_ID",
        description="Default Google Shared Drive target folder ID",
    )

    # Mapping of approved alias names to Shared Drive folder IDs
    allowed_subfolder_map: dict[str, str] = Field(
        default_factory=dict,
        validation_alias="ALLOWED_SUBFOLDER_MAP",
        description="JSON mapping of allowed folder aliases to Shared Drive folder IDs",
    )

    # OAuth scope for Google Drive API
    drive_oauth_scope: str = Field(
        default="https://www.googleapis.com/auth/drive.file",
        validation_alias="DRIVE_OAUTH_SCOPE",
        description="OAuth scope for Application Default Credentials",
    )

    # Maximum payload size in bytes (default: 10 MiB)
    max_content_bytes: int = Field(
        default=10 * 1024 * 1024,
        validation_alias="MAX_CONTENT_BYTES",
        description="Maximum allowed content size in bytes",
    )

    # HTTP server listening port
    port: int = Field(
        default=8080,
        validation_alias="PORT",
        description="HTTP server port for Cloud Run",
    )

    # Logging level
    log_level: str = Field(
        default="INFO",
        validation_alias="LOG_LEVEL",
        description="Logging level (DEBUG, INFO, WARNING, ERROR)",
    )

    @field_validator("allowed_subfolder_map", mode="before")
    @classmethod
    def parse_allowed_subfolder_map(cls, value: Any) -> dict[str, str]:
        """Parse ALLOWED_SUBFOLDER_MAP from JSON string or dictionary."""
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return {}
            try:
                parsed = json.loads(value)
                if not isinstance(parsed, dict):
                    raise ValueError(
                        "ALLOWED_SUBFOLDER_MAP must be a JSON object mapping alias strings to folder IDs"
                    )
                return {str(k).strip(): str(v).strip() for k, v in parsed.items()}
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON format for ALLOWED_SUBFOLDER_MAP: {exc}") from exc
        elif isinstance(value, dict):
            return {str(k).strip(): str(v).strip() for k, v in value.items()}
        return value or {}

    def resolve_folder_id(self, folder_alias: str | None) -> str:
        """Resolve the target Google Shared Drive folder ID.

        Enforces strict fail-closed security:
        - If folder_alias is provided, it MUST exist in ALLOWED_SUBFOLDER_MAP.
        - If folder_alias is omitted, it defaults to DRIVE_FOLDER_ID.
        - Arbitrary folder IDs passed as folder_alias are rejected.

        Raises:
            ValueError: If an unknown alias is supplied or if no folder ID is configured.
        """
        if folder_alias:
            alias = folder_alias.strip()
            if alias in self.allowed_subfolder_map:
                resolved = self.allowed_subfolder_map[alias]
                if not resolved:
                    raise ValueError(f"Configured folder ID for alias '{alias}' is empty.")
                return resolved

            valid_aliases = sorted(list(self.allowed_subfolder_map.keys()))
            alias_list_str = ", ".join(f"'{a}'" for a in valid_aliases) if valid_aliases else "None configured"
            raise ValueError(
                f"Unknown folder_alias '{folder_alias}'. Allowed aliases are: [{alias_list_str}]. "
                "For security, arbitrary Google Drive folder IDs are prohibited. "
                "Configure aliases in the ALLOWED_SUBFOLDER_MAP environment variable."
            )

        # Fall back to default DRIVE_FOLDER_ID
        folder_id = self.drive_folder_id.strip()
        if not folder_id:
            raise ValueError(
                "No target Google Drive folder configured. "
                "DRIVE_FOLDER_ID environment variable is missing or empty, and no valid folder_alias was provided."
            )
        return folder_id


# Cached global settings instance
_settings: Settings | None = None


def get_settings() -> Settings:
    """Retrieve or initialize application settings."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    """Reset cached settings instance (primarily used for unit testing)."""
    global _settings
    _settings = None
