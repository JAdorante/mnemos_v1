# Sparrow gateway — front door for sparrow.ravenry.us.
#
# Tiny next to the app image: it serves two HTML pages, keeps the user table,
# talks to the Docker API to mint seats, and proxies. It never imports the app.
#
#   docker build -f deploy/gateway/Dockerfile -t sparrow-gateway deploy/gateway
FROM python:3.12-slim

WORKDIR /srv/gateway

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY gateway.py store.py provision.py pages.py adopt_seat.py ./
COPY static ./static

# users.json + sessions.json live here — back this volume up, it is the only
# record of who owns which seat.
ENV GATEWAY_DATA_DIR=/srv/gateway/data \
    PYTHONUNBUFFERED=1
VOLUME /srv/gateway/data

EXPOSE 8080
CMD ["uvicorn", "gateway:app", "--host", "0.0.0.0", "--port", "8080", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
