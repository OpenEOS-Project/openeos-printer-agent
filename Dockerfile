FROM python:3.14-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libusb-1.0-0 \
        libjpeg62-turbo \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/
COPY templates/ templates/
# License texts travel with the image (AGPL-3.0-or-later)
COPY LICENSE NOTICE ./

RUN mkdir -p cache/templates config

EXPOSE 8080

ENTRYPOINT ["python", "-m", "src.main"]
CMD ["--config", "/app/config/config.yaml"]
