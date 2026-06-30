FROM python:3.12-alpine

WORKDIR /opt/matrix-tools
COPY app/app.py /opt/matrix-tools/app.py
COPY purge-staged-media.sh /opt/matrix-tools/purge-staged-media.sh
COPY stickerpicker-web /opt/matrix-tools/stickerpicker-web

# 补 cairo 运行库后，TGS/Lottie 转 animated webp 才能正常加载底层渲染链。
RUN apk add --no-cache \
  curl \
  docker-cli \
  postgresql-client \
  ffmpeg \
  cairo \
  python3 \
  py3-pip \
  && pip3 install --no-cache-dir lottie pillow cairosvg \
  && chmod 755 /opt/matrix-tools/app.py /opt/matrix-tools/purge-staged-media.sh

EXPOSE 8190
CMD ["python3", "/opt/matrix-tools/app.py"]
