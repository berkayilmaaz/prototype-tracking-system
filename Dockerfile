FROM python:3.12-slim
WORKDIR /app
COPY server.py auth.py ./
COPY app ./app
COPY seed ./seed
COPY tools ./tools
ENV PT_HOST=0.0.0.0 PT_PORT=8080 PT_DATA=/data PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/api/health',timeout=4)"
CMD ["python", "server.py"]
