# RouteWise application image: the Flask app served by waitress (TLS is added by the Caddy proxy in docker-compose.yml)
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py ./
COPY routewise ./routewise
RUN useradd --create-home routewise
USER routewise
ENV DB_HOST=db BEHIND_PROXY=1 COOKIE_SECURE=1
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=10 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
CMD ["python", "-m", "waitress", "--listen=0.0.0.0:8000", "--threads=8", "app:app"]
