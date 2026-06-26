FROM python:3.12-alpine

WORKDIR /opt/matrix-tools
COPY app/app.py /opt/matrix-tools/app.py
COPY purge-staged-media.sh /opt/matrix-tools/purge-staged-media.sh

RUN apk add --no-cache curl \
  && chmod 755 /opt/matrix-tools/app.py /opt/matrix-tools/purge-staged-media.sh

EXPOSE 8190
CMD ["python3", "/opt/matrix-tools/app.py"]
