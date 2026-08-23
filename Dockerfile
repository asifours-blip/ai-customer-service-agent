FROM python:3.11-slim

WORKDIR /srv

COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --no-cache-dir .

# 运行时数据与初始化链所需（entrypoint 会用到）
COPY alembic.ini ./
COPY migrations ./migrations
COPY scripts/seed_db.py scripts/ingest_docs.py ./scripts/
COPY knowledge_base ./knowledge_base
COPY policy ./policy
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
