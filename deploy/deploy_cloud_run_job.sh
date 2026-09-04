#!/usr/bin/env bash
set -euo pipefail

: "${IMAGE_URI:?Set IMAGE_URI to the immutable container image URI}"

PILOT_ARGS="^|^scripts/data/cloud_pilot_qualification.py|--provider|theta|--start|2024-01-02|--end|2024-03-28|--symbols|TQQQ,SQQQ|--qualify|--bucket|kairo-market-artifacts-507516|--manifest-object|manifests/theta_q1_2024_manifest.json|--storage-root|/mnt/kairo-market-artifacts/historical-market|--authorize-paid-theta-history"

gcloud run jobs deploy kairo-historical-ingestion \
  --project=kairo-research-507516 \
  --region=us-south1 \
  --image="${IMAGE_URI}" \
  --set-cloudsql-instances=kairo-research-507516:us-south1:kairo-research-db \
  --set-secrets=THETADATA_API_KEY=thetadata-api-key:latest,KAIRO_RUNTIME_DATABASE_URL=kairo-runtime-db-url:latest \
  --set-env-vars=KAIRO_ARTIFACT_BUCKET=kairo-market-artifacts-507516,KAIRO_CLOUD_PILOT_AUTHORIZED=1 \
  --add-volume=name=kairo-market-artifacts,type=cloud-storage,bucket=kairo-market-artifacts-507516 \
  --add-volume-mount=volume=kairo-market-artifacts,mount-path=/mnt/kairo-market-artifacts \
  --command=python \
  --args="${PILOT_ARGS}"
