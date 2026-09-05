FROM python:3.12-slim

WORKDIR /app

COPY webapp/backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# The src/ package this backend imports directly - one source of truth with
# the Hermes-scheduled cron jobs, not a reimplementation.
COPY src/ /app/src/
COPY sources.json /app/sources.json
COPY webapp/backend/ /app/backend/
COPY webapp/frontend/ /app/webapp/frontend/

EXPOSE 9926

CMD ["python3", "-m", "uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "9926"]
