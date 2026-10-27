FROM python:3.11-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libusb-1.0-0 \
        libjpeg62-turbo \
        iproute2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/

RUN mkdir -p config /var/lib/openeos-printer /var/log/openeos-printer

EXPOSE 8080

ENTRYPOINT ["python", "-m", "src.main"]
CMD ["run", "--config", "/app/config/config.yaml"]
