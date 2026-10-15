FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/model-onbellegi \
    HF_HUB_DISABLE_SYMLINKS_WARNING=1

WORKDIR /app

# PyPI'daki Linux torch paketi CUDA'lı (~2 GB); embedding modeli CPU'da çalıştığı için
# CPU sürümü yeterli (~200 MB).
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu

# Önce yalnızca bağımlılıklar: kod değiştiğinde bu katman önbellekten gelir. pyproject README'yi
# yalnızca paket meta verisi için okur; gerçek README kopyalansaydı her README düzenlemesi bu
# katmanı bozup bütün paketleri (scipy, transformers...) yeniden indirtirdi. Boş dosya yeter.
COPY pyproject.toml ./
COPY app/__init__.py app/__init__.py
RUN touch README.md && pip install -e .

COPY app ./app
COPY scripts ./scripts
COPY sql ./sql
COPY data/kilavuzlar/kilavuzlar.json data/kilavuzlar/*.pdf ./data/kilavuzlar/

RUN useradd --create-home andon && mkdir /model-onbellegi && chown andon /model-onbellegi
USER andon

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=180s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/saglik')"

CMD ["sh", "-c", "python -m scripts.hazirla && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
