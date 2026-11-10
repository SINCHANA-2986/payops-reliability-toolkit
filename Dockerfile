FROM python:3.12-slim
WORKDIR /srv
RUN pip install --no-cache-dir fastapi "uvicorn[standard]" redis prometheus_client httpx
COPY app ./app
COPY scripts/load_gen.py ./scripts/load_gen.py
COPY drills/results ./drills/results
EXPOSE 8099
HEALTHCHECK --interval=10s --timeout=2s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8099/ready', timeout=2).status==200 else 1)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8099", "--log-level", "warning"]
