#!/usr/bin/env bash
set -euo pipefail

: "${IMAGE_URI:?Set IMAGE_URI to the immutable container image URI}"

gcloud run jobs deploy kairo-historical-ingestion \
  --project=kairo-research-507516 \
  --region=us-south1 \
  --image="${IMAGE_URI}" \
  --set-cloudsql-instances=kairo-research-507516:us-south1:kairo-research-db \
  --set-secrets=THETADATA_API_KEY=thetadata-api-key:latest,KAIRO_RUNTIME_DATABASE_URL=kairo-runtime-db-url:latest \
  --set-env-vars=KAIRO_ARTIFACT_BUCKET=kairo-market-artifacts-507516 \
  --command=python \
  --args=scripts/data/cloud_smoke_test.py
