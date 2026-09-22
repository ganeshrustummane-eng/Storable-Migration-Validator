# Deploying to Google Cloud Platform

This guide covers deploying the Migration Intelligence Connector (the FastAPI server that
exposes the 24 tools to Gemini Enterprise) to **Cloud Run**, plus an optional deployment of the
Streamlit review UI (`webapp/app.py`) as a second Cloud Run service.

Cloud Run is the recommended target: the connector is stateless HTTP (auth on every request,
no server-side session), scales to zero when idle, and Gemini Enterprise reaches it over a
plain HTTPS URL — no VPC peering or GKE cluster required for the hackathon submission.

---

## 1. Architecture on GCP

```
Gemini Enterprise
      │  HTTPS + Bearer token
      ▼
Cloud Run: migration-connector      (FastAPI, src/gemini_connector/api.py)
      │
      ├─ Secret Manager             (DB credentials, GOOGLE_API_KEY, CONNECTOR_API_TOKEN)
      ├─ Cloud Logging              (stdout/stderr from uvicorn + audit log lines)
      │
      ▼
Source databases (PostgreSQL / MSSQL / Athena)  +  Snowflake
      (via Cloud SQL Auth Proxy, VPC connector, or public endpoint + IP allowlist,
       depending on where each source actually lives)

Cloud Run: migration-webapp         (Streamlit, webapp/app.py)  — optional, human reviewers only
```

Both services can share the same container image (`Dockerfile` at repo root) with a different
`CMD` override, or you can build the webapp as a second, near-identical image.

---

## 2. Prerequisites

- A GCP project with billing enabled and the following APIs on:
  ```bash
  gcloud services enable run.googleapis.com \
      artifactregistry.googleapis.com \
      secretmanager.googleapis.com \
      cloudbuild.googleapis.com
  ```
- `gcloud` CLI authenticated (`gcloud auth login`) and pointed at the target project:
  ```bash
  gcloud config set project YOUR_PROJECT_ID
  gcloud config set run/region YOUR_REGION   # e.g. us-central1
  ```
- Network reachability from Cloud Run to your source databases and Snowflake — either public
  endpoints with IP allowlisting for Cloud Run's egress, or a
  [Serverless VPC Access connector](https://cloud.google.com/run/docs/configuring/vpc-connectors)
  if the sources sit inside a private VPC.

---

## 3. Store secrets in Secret Manager

Never bake credentials into the image or commit them. Create one secret per sensitive value
(matching the `.env` keys the connector already reads):

```bash
printf '%s' "your-gemini-api-key"      | gcloud secrets create GOOGLE_API_KEY --data-file=-
printf '%s' "your-connector-token"     | gcloud secrets create CONNECTOR_API_TOKEN --data-file=-
printf '%s' "your-snowflake-password"  | gcloud secrets create SNOWFLAKE_PASSWORD --data-file=-
printf '%s' "your-source-db-password"  | gcloud secrets create SRC_1_PASSWORD --data-file=-
# Repeat for every SRC_N_PASSWORD / SNOWFLAKE_* secret your .env currently holds.
```

Grant the Cloud Run service account access to read them:

```bash
PROJECT_NUMBER=$(gcloud projects describe YOUR_PROJECT_ID --format='value(projectNumber)')
SERVICE_ACCOUNT="$PROJECT_NUMBER-compute@developer.gserviceaccount.com"

for SECRET in GOOGLE_API_KEY CONNECTOR_API_TOKEN SNOWFLAKE_PASSWORD SRC_1_PASSWORD; do
  gcloud secrets add-iam-policy-binding "$SECRET" \
      --member="serviceAccount:${SERVICE_ACCOUNT}" \
      --role="roles/secretmanager.secretAccessor"
done
```

Non-secret configuration (hosts, ports, usernames, `AUTH_MODE`, `CONNECTOR_ROLES`, the
Snowflake account/warehouse/role) can be passed as plain environment variables at deploy time —
they don't need Secret Manager.

---

## 4. Build and push the image

```bash
gcloud artifacts repositories create migration-validator \
    --repository-format=docker --location=YOUR_REGION

gcloud builds submit --tag \
    YOUR_REGION-docker.pkg.dev/YOUR_PROJECT_ID/migration-validator/connector:latest .
```

This uses the `Dockerfile` at the repo root, which installs the MSSQL ODBC driver required by
`pyodbc` in addition to `requirements.txt`.

---

## 5. Deploy the connector to Cloud Run

```bash
gcloud run deploy migration-connector \
  --image YOUR_REGION-docker.pkg.dev/YOUR_PROJECT_ID/migration-validator/connector:latest \
  --platform managed \
  --region YOUR_REGION \
  --allow-unauthenticated \
  --set-env-vars AUTH_MODE=static,CONNECTOR_ROLES=ADMIN,SNOWFLAKE_ACCOUNT=your-account,SNOWFLAKE_USERNAME=your-user,SNOWFLAKE_WAREHOUSE=your-warehouse,SNOWFLAKE_ROLE=your-role,SRC_1_TYPE=postgresql,SRC_1_HOST=your-postgres-host,SRC_1_PORT=5432,SRC_1_USERNAME=your-user \
  --set-secrets GOOGLE_API_KEY=GOOGLE_API_KEY:latest,CONNECTOR_API_TOKEN=CONNECTOR_API_TOKEN:latest,SNOWFLAKE_PASSWORD=SNOWFLAKE_PASSWORD:latest,SRC_1_PASSWORD=SRC_1_PASSWORD:latest
```

`--allow-unauthenticated` exposes the URL publicly — this is safe here because the connector
enforces its **own** bearer-token auth (`AUTH_MODE`) on every write endpoint per
[`docs/api/authentication.md`](authentication.md). If you'd rather rely on Cloud Run's IAM
instead of (or in addition to) the app-level token, drop that flag and grant
`roles/run.invoker` to the identity Gemini Enterprise will authenticate as.

Note the deployed URL, e.g. `https://migration-connector-xxxxx-uc.a.run.app` — this is what you
register with Gemini Enterprise as the connector endpoint (see
[`docs/architecture/gemini-integration.md`](../architecture/gemini-integration.md), section
"Register as a Gemini Extension").

If your source databases or Snowflake are reachable only from inside a private network, add:

```bash
  --vpc-connector YOUR_VPC_CONNECTOR_NAME \
  --vpc-egress private-ranges-only
```

---

## 6. (Optional) Deploy the Streamlit review UI

Human reviewers use `webapp/app.py` for the Review & Approve workflow. Deploy it as a second
Cloud Run service pointed at the same connector:

```bash
gcloud run deploy migration-webapp \
  --image YOUR_REGION-docker.pkg.dev/YOUR_PROJECT_ID/migration-validator/connector:latest \
  --command streamlit \
  --args run,webapp/app.py,--server.port=8080,--server.address=0.0.0.0,--server.headless=true \
  --platform managed \
  --region YOUR_REGION \
  --port 8080 \
  --no-allow-unauthenticated \
  --set-env-vars CONNECTOR_URL=https://migration-connector-xxxxx-uc.a.run.app,SNOWFLAKE_ACCOUNT=your-account,SNOWFLAKE_USERNAME=your-user,SNOWFLAKE_WAREHOUSE=your-warehouse,SNOWFLAKE_ROLE=your-role \
  --set-secrets SNOWFLAKE_PASSWORD=SNOWFLAKE_PASSWORD:latest,GOOGLE_API_KEY=GOOGLE_API_KEY:latest
```

`--no-allow-unauthenticated` is deliberate: this UI performs approvals and write-backs, so it
should sit behind Cloud Run IAM (`gcloud run services add-iam-policy-binding ... --role
roles/run.invoker`) or [Identity-Aware Proxy](https://cloud.google.com/iap), not the public
internet — reviewer identity is still captured inside the app, but IAM adds a second gate before
anyone reaches the login box.

---

## 7. Verify the deployment

```bash
CONNECTOR_URL=$(gcloud run services describe migration-connector --region YOUR_REGION --format='value(status.url)')

curl "$CONNECTOR_URL/health"
curl "$CONNECTOR_URL/tools" | head -c 500
curl -X POST "$CONNECTOR_URL/tools/get_migration_summary" \
     -H "Authorization: Bearer YOUR_CONNECTOR_API_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"arguments": {"layer": "bronze"}}'
```

These are the same checks as [`SUBMISSION_CHECKLIST.md`](../../SUBMISSION_CHECKLIST.md) under
"Gemini Integration" — run them against the Cloud Run URL instead of `localhost:8001` once
deployed.

---

## 8. Cost and scaling notes

- Cloud Run scales to zero — the connector costs nothing while idle, which matters for a
  hackathon demo that isn't under constant load.
- Set `--min-instances 1` on the connector service before a live demo/judging session to avoid
  cold-start latency on the first Gemini call.
- `--memory 512Mi --cpu 1` is sufficient for the connector; the Streamlit UI benefits from
  `--memory 1Gi` if you're rendering large coverage tables.

---

## 9. Rollback

Cloud Run keeps every revision. To roll back:

```bash
gcloud run services update-traffic migration-connector --to-revisions=PREVIOUS_REVISION=100
```
