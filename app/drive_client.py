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

"""Google Drive API v3 client wrapper for writing files to Google Shared Drives."""

import io
import json
import logging
import re
from pathlib import PurePath
from typing import Any, Literal
import google.auth
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload
from pydantic import BaseModel, Field

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

# File extension mappings for supported formats
FORMAT_EXTENSIONS: dict[str, str] = {
    "txt": ".txt",
    "md": ".md",
    "csv": ".csv",
    "json": ".json",
    "google_doc": "",  # Google Docs do not require an extension
}

# Upload media MIME type mappings
FORMAT_UPLOAD_MIME_TYPES: dict[str, str] = {
    "txt": "text/plain; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
    "csv": "text/csv; charset=utf-8",
    "json": "application/json; charset=utf-8",
    "google_doc": "text/markdown; charset=utf-8",
}

# Target Drive metadata MIME type (for Google Doc conversion)
GOOGLE_DOCS_MIME_TYPE = "application/vnd.google-apps.document"


class SaveDriveFileResult(BaseModel):
    """Structured result returned by save_drive_file tool."""

    status: Literal["created", "updated"] = Field(
        ..., description="Action performed: 'created' if a new file was written, 'updated' if existing was overwritten"
    )
    file_id: str = Field(..., description="Google Drive file ID")
    file_name: str = Field(..., description="Name of the file in Google Drive")
    mime_type: str = Field(..., description="MIME type of the file in Google Drive")
    web_view_link: str = Field(..., description="URL to open and view the file in Google Drive")
    folder_id: str = Field(..., description="Google Shared Drive folder ID where the file is stored")


def sanitize_filename(file_name: str, output_format: str) -> str:
    """Sanitize filename to prevent path traversal and control character issues.

    - Removes path traversal patterns (e.g. '..')
    - Replaces path separators ('/' and '\\') with '_'
    - Strips non-printable and control characters
    - Strips leading and trailing dots, underscores, and spaces
    - Appends extension matching output_format if no extension is present
    """
    if not file_name or not file_name.strip():
        raise ValueError("file_name cannot be empty or whitespace-only.")

    # Strip control characters, non-printable characters, and null bytes
    cleaned = "".join(c for c in file_name if c.isprintable() and ord(c) >= 32).strip()

    # Eliminate directory traversal sequences
    cleaned = re.sub(r"\.\.+", "", cleaned)

    # Convert path separators to underscores
    cleaned = re.sub(r"[\\/]+", "_", cleaned)

    # Strip leading/trailing dots, underscores, and whitespace
    cleaned = cleaned.strip("._ ")

    if not cleaned:
        raise ValueError(f"Invalid file_name '{file_name}': empty after path sanitization.")

    # Append extension matching output_format if none is present
    format_ext = FORMAT_EXTENSIONS.get(output_format, "")
    if format_ext and not PurePath(cleaned).suffix:
        cleaned = f"{cleaned}{format_ext}"

    return cleaned


def format_drive_http_error(err: HttpError, target_folder_id: str) -> RuntimeError:
    """Map Google Drive API errors into clear, actionable messages."""
    status = getattr(err.resp, "status", None)
    content_str = ""
    if isinstance(err.content, bytes):
        content_str = err.content.decode("utf-8", errors="replace")
    elif isinstance(err.content, str):
        content_str = err.content

    logger.error("Drive API HttpError status=%s content=%s", status, content_str)

    # 403 storageQuotaExceeded
    if "storageQuotaExceeded" in content_str:
        return RuntimeError(
            f"Google Drive API error (403 storageQuotaExceeded): Storage quota exceeded. "
            f"GCP service accounts have 0 bytes of personal Drive quota. "
            f"The destination folder ID '{target_folder_id}' MUST be inside a Google Shared Drive "
            f"(not personal 'My Drive'), and the Cloud Run service account must be added to that Shared Drive "
            f"as a member with at least 'Contributor' role."
        )

    # 404 notFound
    if status == 404 or "notFound" in content_str:
        return RuntimeError(
            f"Google Drive API error (404 notFound): The folder or file with ID '{target_folder_id}' was not found. "
            f"Verify that the Google Shared Drive folder exists, the ID is correct, "
            f"and the Cloud Run service account has been added as a member of the Shared Drive."
        )

    # 403 Forbidden / insufficient permissions
    if status == 403:
        return RuntimeError(
            f"Google Drive API error (403 Forbidden): Access denied for target folder ID '{target_folder_id}'. "
            f"Ensure the Cloud Run service account has been added as a member to the Google Shared Drive "
            f"with 'Contributor' (or 'Content manager') role. Details: {content_str}"
        )

    # 401 Unauthorized
    if status == 401:
        return RuntimeError(
            f"Google Drive API error (401 Unauthorized): Application Default Credentials (ADC) "
            f"are missing, expired, or invalid. Details: {content_str}"
        )

    return RuntimeError(f"Google Drive API error ({status}): {err}")


class DriveClient:
    """Encapsulated Google Drive API v3 client for Shared Drive writes."""

    def __init__(self, settings: Settings | None = None, service: Any = None):
        self.settings = settings or get_settings()
        self._service = service

    def _get_service(self) -> Any:
        """Lazily initialize Google Drive v3 client via Application Default Credentials (ADC)."""
        if self._service is None:
            logger.info("Initializing Google Drive API client using ADC with scope %s", self.settings.drive_oauth_scope)
            credentials, _ = google.auth.default(scopes=[self.settings.drive_oauth_scope])
            self._service = build("drive", "v3", credentials=credentials, cache_discovery=False)
        return self._service

    def save_file(
        self,
        file_name: str,
        content: str,
        output_format: Literal["txt", "md", "csv", "json", "google_doc"] = "txt",
        folder_alias: str | None = None,
        on_conflict: Literal["create_new", "overwrite"] = "create_new",
    ) -> SaveDriveFileResult:
        """Save file into Google Shared Drive.

        Args:
            file_name: Target file name (sanitized and validated).
            content: UTF-8 file content.
            output_format: Output format ('txt', 'md', 'csv', 'json', 'google_doc').
            folder_alias: Optional alias from ALLOWED_SUBFOLDER_MAP.
            on_conflict: Conflict resolution ('create_new' or 'overwrite').

        Returns:
            SaveDriveFileResult with file metadata and web view URL.
        """
        # 1. Validate content
        if not content or not content.strip():
            raise ValueError("content cannot be empty or whitespace-only.")

        content_bytes = content.encode("utf-8")
        if len(content_bytes) > self.settings.max_content_bytes:
            raise ValueError(
                f"Content size ({len(content_bytes)} bytes) exceeds configured "
                f"MAX_CONTENT_BYTES ({self.settings.max_content_bytes} bytes)."
            )

        # 2. Resolve target Shared Drive folder ID
        target_folder_id = self.settings.resolve_folder_id(folder_alias)

        # 3. Sanitize file name
        sanitized_name = sanitize_filename(file_name, output_format)

        # 4. Determine MIME types
        upload_mime = FORMAT_UPLOAD_MIME_TYPES.get(output_format, "text/plain; charset=utf-8")
        media = MediaIoBaseUpload(io.BytesIO(content_bytes), mimetype=upload_mime, resumable=False)

        service = self._get_service()

        try:
            # Overwrite logic
            if on_conflict == "overwrite":
                # Look for existing active file with exact name in target Shared Drive folder
                escaped_name = sanitized_name.replace("\\", "\\\\").replace("'", "\\'")
                query = f"'{target_folder_id}' in parents and name = '{escaped_name}' and trashed = false"

                response = service.files().list(
                    q=query,
                    fields="files(id, name, mimeType, webViewLink, parents)",
                    spaces="drive",
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                ).execute()

                existing_files = response.get("files", [])
                if existing_files:
                    existing = existing_files[0]
                    file_id = existing["id"]
                    logger.info("Overwriting existing file id=%s name=%s", file_id, sanitized_name)

                    update_body: dict[str, Any] = {}
                    if output_format == "google_doc":
                        update_body["mimeType"] = GOOGLE_DOCS_MIME_TYPE

                    updated = service.files().update(
                        fileId=file_id,
                        body=update_body if update_body else None,
                        media_body=media,
                        fields="id, name, mimeType, webViewLink, parents",
                        supportsAllDrives=True,
                    ).execute()

                    return SaveDriveFileResult(
                        status="updated",
                        file_id=updated["id"],
                        file_name=updated["name"],
                        mime_type=updated.get("mimeType", existing.get("mimeType", "application/octet-stream")),
                        web_view_link=updated.get("webViewLink", existing.get("webViewLink", f"https://drive.google.com/file/d/{file_id}/view")),
                        folder_id=target_folder_id,
                    )

            # Create new file (either on_conflict == 'create_new' or file did not exist)
            logger.info("Creating new file name=%s in folder_id=%s", sanitized_name, target_folder_id)
            file_metadata: dict[str, Any] = {
                "name": sanitized_name,
                "parents": [target_folder_id],
            }
            if output_format == "google_doc":
                file_metadata["mimeType"] = GOOGLE_DOCS_MIME_TYPE

            created = service.files().create(
                body=file_metadata,
                media_body=media,
                fields="id, name, mimeType, webViewLink, parents",
                supportsAllDrives=True,
            ).execute()

            return SaveDriveFileResult(
                status="created",
                file_id=created["id"],
                file_name=created["name"],
                mime_type=created.get("mimeType", "application/octet-stream"),
                web_view_link=created.get("webViewLink", f"https://drive.google.com/file/d/{created['id']}/view"),
                folder_id=target_folder_id,
            )

        except HttpError as err:
            raise format_drive_http_error(err, target_folder_id) from err
