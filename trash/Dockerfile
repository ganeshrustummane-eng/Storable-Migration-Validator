# Migration Intelligence Connector — container image for Cloud Run / GKE.
# Builds the FastAPI connector that exposes Migration Validator's 24 tools to Gemini Enterprise.
# The Streamlit UI (webapp/app.py) can run from the same image — see docs/deployment/gcp-deployment.md.

# Pinned to -bookworm (Debian 12), not the floating `python:3.11-slim` tag —
# that tag recently moved to Debian 13 "trixie", which removed `apt-key`
# entirely and broke the Microsoft repo setup below with "apt-key: not found".
# Pinning avoids the base image silently changing out from under this build again.
FROM python:3.11-slim-bookworm

# unixODBC + msodbcsql18 are required by pyodbc for the MSSQL source connector.
# Must be msodbcsql18 (Driver 18), not 17 — the connection code throughout this
# repo (factory.py, setup_wizard.py, extractors.py) defaults to
# "ODBC Driver 18 for SQL Server", and installing 17 here caused a live
# "Can't open lib 'ODBC Driver 18 for SQL Server' : file not found" error when
# Gemini Enterprise called list_tables against SRC_2 in production.
# Modern keyring-based repo setup (apt-key is deprecated/removed) — the GPG
# key is dearmored into /usr/share/keyrings/ and referenced via signed-by=
# instead of being added to the global trusted keyring.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl gnupg unixodbc unixodbc-dev \
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
    && curl -fsSL https://packages.microsoft.com/config/debian/12/prod.list -o /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Cloud Run injects PORT; the connector reads it via CMD below.
ENV PORT=8001
EXPOSE 8001

CMD ["sh", "-c", "uvicorn src.connector.api:app --host 0.0.0.0 --port ${PORT}"]
