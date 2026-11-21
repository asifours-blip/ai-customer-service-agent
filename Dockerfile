FROM python:3.11-slim

WORKDIR /srv

COPY pyproject.toml uv.lock README.md ./
COPY app ./app
RUN python -m pip install --no-cache-dir uv==0.13.5 \
    && uv sync --locked --no-dev --no-editable --no-cache --python /usr/local/bin/python
ENV PATH="/srv/.venv/bin:${PATH}"

# 运行时数据与初始化链所需（entrypoint 会用到）
COPY alembic.ini ./
COPY migrations ./migrations
COPY scripts/seed_db.py scripts/bootstrap_kb.py ./scripts/
COPY knowledge_base ./knowledge_base
COPY kb_smoke_queries.yaml ./
COPY policy ./policy
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
