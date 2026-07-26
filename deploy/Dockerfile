# One image for all three services: ingest needs ffmpeg, uploader needs boto3,
# web needs uvicorn. Building three would mean three downloads of the same base,
# and a second image built FROM the first would depend on compose's build order,
# which compose does not guarantee.
FROM alpine:3

RUN apk add --no-cache ffmpeg python3 py3-pip \
 && pip install --break-system-packages --no-cache-dir boto3 fastapi uvicorn

WORKDIR /app
COPY src/ /app/src/
ENV PYTHONPATH=/app/src PYTHONUNBUFFERED=1
