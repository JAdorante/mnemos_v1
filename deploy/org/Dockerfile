# Org Record Service (records layer). Same image for every deployment rung —
# Connected, Sealed and Perimeter differ only in configuration.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv/org
COPY requirements-org.txt .
RUN pip install --no-cache-dir -r requirements-org.txt
COPY org_coordinator ./org_coordinator
# The canonical-hash module is shared with nodes (stdlib only); nothing else
# from app/ is imported by the service.
COPY app/__init__.py ./app/__init__.py
COPY app/services/__init__.py ./app/services/__init__.py
COPY app/services/records/__init__.py ./app/services/records/__init__.py
COPY app/services/records/canonical.py ./app/services/records/canonical.py
COPY scripts/verify_audit_chain.py ./scripts/verify_audit_chain.py
EXPOSE 8100
CMD ["sh", "-c", "python -m org_coordinator.records migrate && uvicorn org_coordinator.main:app --host 0.0.0.0 --port 8100"]
