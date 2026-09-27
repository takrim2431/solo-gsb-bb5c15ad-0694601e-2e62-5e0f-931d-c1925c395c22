FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH

WORKDIR /app

# 非 root 用户；/opt/venv 归属该用户，使依赖可在容器启动时安装。
RUN useradd --create-home --uid 10001 appuser \
    && python -m venv "$VIRTUAL_ENV" \
    && chown -R appuser:appuser /opt/venv /app

COPY --chown=appuser:appuser requirements.txt ./requirements.txt
COPY --chown=appuser:appuser docker/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY --chown=appuser:appuser app ./app

RUN chmod +x /usr/local/bin/entrypoint.sh

USER appuser
EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
