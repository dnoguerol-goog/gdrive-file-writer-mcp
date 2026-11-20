#!/usr/bin/env bash
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
# ==============================================================================
# deploy.sh - Parameterized Cloud Run deployment for Gemini Enterprise MCP Server
# ==============================================================================
set -euo pipefail

# Disable interactive prompts in gcloud
export CLOUDSDK_CORE_INTERACTIVE_UX_STYLE=OFF
export CLOUDSDK_CORE_DISABLE_PROMPTS=1

echo "=========================================================="
echo " Google Shared Drive File Writer MCP - Cloud Run Deploy "
echo "=========================================================="

# ------------------------------------------------------------------------------
# 1. Validate Required Environment Variables
# ------------------------------------------------------------------------------
MISSING_VARS=0

if [[ -z "${PROJECT_ID:-}" ]]; then
  echo "[-] ERROR: PROJECT_ID is required (e.g. export PROJECT_ID='my-gcp-project')" >&2
  MISSING_VARS=1
fi

if [[ -z "${GE_PROJECT_NUMBER:-}" ]]; then
  echo "[-] ERROR: GE_PROJECT_NUMBER is required (Gemini Enterprise GCP project number, e.g. export GE_PROJECT_NUMBER='123456789012')" >&2
  MISSING_VARS=1
fi

if [[ -z "${REGION:-}" ]]; then
  echo "[-] ERROR: REGION is required (e.g. export REGION='us-central1')" >&2
  MISSING_VARS=1
fi

if [[ -z "${DRIVE_FOLDER_ID:-}" ]]; then
  echo "[-] ERROR: DRIVE_FOLDER_ID is required (Google Shared Drive folder ID, e.g. export DRIVE_FOLDER_ID='0Axxxxxxxxx')" >&2
  MISSING_VARS=1
fi

if [[ "${MISSING_VARS}" -ne 0 ]]; then
  echo ""
  echo "Usage Example:"
  echo "  export PROJECT_ID=\"my-cloud-run-project\""
  echo "  export GE_PROJECT_NUMBER=\"123456789012\""
  echo "  export REGION=\"us-central1\""
  echo "  export DRIVE_FOLDER_ID=\"0AB1c2D3e4F5\""
  echo "  # Optional:"
  echo "  export SERVICE_NAME=\"ge-drive-file-mcp\""
  echo "  export ALLOWED_SUBFOLDER_MAP='{\"monthly\":\"0AB1111\",\"finance\":\"0AB2222\"}'"
  echo "  export DRIVE_OAUTH_SCOPE=\"https://www.googleapis.com/auth/drive.file\""
  echo "  ./deploy.sh"
  exit 1
fi

SERVICE_NAME="${SERVICE_NAME:-ge-drive-file-mcp}"
DRIVE_OAUTH_SCOPE="${DRIVE_OAUTH_SCOPE:-https://www.googleapis.com/auth/drive.file}"
ALLOWED_SUBFOLDER_MAP="${ALLOWED_SUBFOLDER_MAP:-}"
SA_NAME="drive-file-writer"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
GE_INVOKER_SA="serviceAccount:service-${GE_PROJECT_NUMBER}@gcp-sa-discoveryengine.iam.gserviceaccount.com"

echo "[+] Target Project:       ${PROJECT_ID}"
echo "[+] Gemini Ent. Project:  ${GE_PROJECT_NUMBER}"
echo "[+] Cloud Run Region:     ${REGION}"
echo "[+] Service Name:         ${SERVICE_NAME}"
echo "[+] Shared Drive Folder:  ${DRIVE_FOLDER_ID}"
echo "[+] Runtime Service Acct: ${SA_EMAIL}"
echo "[+] Discovery Engine SA:  ${GE_INVOKER_SA}"

# ------------------------------------------------------------------------------
# 2. Create Least-Privilege Dedicated Runtime Service Account
# ------------------------------------------------------------------------------
echo ""
echo "[+] Checking runtime service account..."
if gcloud iam service-accounts describe "${SA_EMAIL}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  echo "[+] Service account ${SA_EMAIL} already exists."
else
  echo "[+] Creating dedicated service account: ${SA_NAME}..."
  gcloud iam service-accounts create "${SA_NAME}" \
    --project="${PROJECT_ID}" \
    --display-name="Gemini Enterprise Drive File Writer Cloud Run SA" \
    --description="Least-privilege runtime identity for Drive File Writer MCP server (zero project IAM roles required)" \
    --quiet
  echo "[+] Created ${SA_EMAIL}."
fi

# ------------------------------------------------------------------------------
# 3. Build & Deploy Cloud Run Service Privately
# ------------------------------------------------------------------------------
echo ""
echo "[+] Deploying Cloud Run service privately (--no-allow-unauthenticated)..."

# Construct environment variables string
ENV_VARS="DRIVE_FOLDER_ID=${DRIVE_FOLDER_ID},DRIVE_OAUTH_SCOPE=${DRIVE_OAUTH_SCOPE}"
if [[ -n "${ALLOWED_SUBFOLDER_MAP}" ]]; then
  ENV_VARS="${ENV_VARS},ALLOWED_SUBFOLDER_MAP=${ALLOWED_SUBFOLDER_MAP}"
fi

gcloud run deploy "${SERVICE_NAME}" \
  --source . \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --service-account="${SA_EMAIL}" \
  --no-allow-unauthenticated \
  --set-env-vars="${ENV_VARS}" \
  --port=8080 \
  --min-instances=0 \
  --max-instances=10 \
  --cpu=1 \
  --memory=512Mi \
  --timeout=300 \
  --quiet

# Retrieve the assigned Cloud Run URL
SERVICE_URL=$(gcloud run services describe "${SERVICE_NAME}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --format="value(status.url)")

echo "[+] Deployed successfully to: ${SERVICE_URL}"

# ------------------------------------------------------------------------------
# 4. Authorize Gemini Enterprise Discovery Engine Service Agent
# ------------------------------------------------------------------------------
echo ""
echo "[+] Granting roles/run.invoker to Gemini Enterprise Discovery Engine service agent..."
gcloud run services add-iam-policy-binding "${SERVICE_NAME}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --member="${GE_INVOKER_SA}" \
  --role="roles/run.invoker" \
  --quiet

echo "[+] Ingress authorization configured."

# ------------------------------------------------------------------------------
# 5. Output Gemini Enterprise Registration Details & Sharing Instructions
# ------------------------------------------------------------------------------
echo ""
echo "=========================================================================="
echo "                          DEPLOYMENT COMPLETE                             "
echo "=========================================================================="
echo ""
echo "1. GEMINI ENTERPRISE MCP DATA STORE REGISTRATION:"
echo "   - Connection URL:     ${SERVICE_URL}/mcp"
echo "   - Health Check URL:   ${SERVICE_URL}/healthz"
echo "   - Authentication:     Select 'No authentication' (Cloud Run IAM handles"
echo "                         authentication automatically via X-Serverless-Authorization)"
echo ""
echo "2. CRITICAL GOOGLE SHARED DRIVE ACCESS (REQUIRED):"
echo "   Service accounts have ZERO personal Google Drive quota."
echo "   You MUST add the runtime service account to your Google Shared Drive:"
echo "   - Open Google Drive -> Navigate to your Shared Drive."
echo "   - Click 'Manage members' (or right-click folder -> Share)."
echo "   - Add Member: ${SA_EMAIL}"
echo "   - Role: 'Contributor' (or 'Content manager')"
echo "   - Target Folder ID: ${DRIVE_FOLDER_ID}"
echo ""
echo "=========================================================================="
