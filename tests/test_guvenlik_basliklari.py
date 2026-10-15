"""Güvenlik başlıkları her yanıtta: sayfa, statik dosya, API, hata. Veritabanı gerektirmez."""

import pytest
from fastapi.testclient import TestClient

from app.guvenlik_basliklari import CSP
from app.main import uygulama_olustur


@pytest.fixture(scope="module")
def istemci_dbsiz():
    # `with` yok: lifespan (bağlantı havuzu, model yükleme) çalışmaz.
    return TestClient(uygulama_olustur(model_on_yukle=False))


def _direktifler(csp: str) -> dict[str, list[str]]:
    return {p.split()[0]: p.split()[1:] for p in csp.split("; ")}


@pytest.mark.parametrize(
    ("yol", "durum"),
    [
        ("/", 200),
        ("/static/app.js", 200),
        ("/static/style.css", 200),
        ("/openapi.json", 200),
        ("/olmayan-sayfa", 404),
        ("/static/olmayan.js", 404),
    ],
)
def test_her_yanitta_guvenlik_basliklari(istemci_dbsiz, yol, durum):
    cevap = istemci_dbsiz.get(yol)
    assert cevap.status_code == durum
    assert cevap.headers["x-content-type-options"] == "nosniff"
    assert cevap.headers["x-frame-options"] == "DENY"
    assert cevap.headers["referrer-policy"] == "no-referrer"
    assert "camera=()" in cevap.headers["permissions-policy"]
    assert cevap.headers["content-security-policy"] == CSP


def test_csp_satir_ici_ve_dis_kaynaga_izin_vermez():
    d = _direktifler(CSP)
    for direktif in ("default-src", "script-src", "style-src", "connect-src"):
        assert d[direktif] == ["'self'"], direktif
    assert "'unsafe-inline'" not in CSP and "'unsafe-eval'" not in CSP and "*" not in CSP
    assert d["object-src"] == ["'none'"]
    assert d["frame-ancestors"] == ["'none'"]
    assert d["base-uri"] == ["'none'"]
    assert d["img-src"] == ["'self'", "blob:"]  # data: yok; sekme ikonu dosyadan gelir


@pytest.mark.parametrize("yol", ["/docs", "/redoc"])
def test_swagger_sayfalari_csp_disinda_ama_diger_basliklar_var(istemci_dbsiz, yol):
    # Swagger UI ve ReDoc CDN'den script yükler; sıkı CSP onları kırardı.
    cevap = istemci_dbsiz.get(yol)
    assert cevap.status_code == 200
    assert "content-security-policy" not in cevap.headers
    assert cevap.headers["x-content-type-options"] == "nosniff"


def test_uygulamanin_kendi_basligi_ezilmez():
    from fastapi import FastAPI
    from fastapi.responses import PlainTextResponse

    from app.guvenlik_basliklari import GuvenlikBasliklari

    uygulama = FastAPI()
    uygulama.add_middleware(GuvenlikBasliklari)

    @uygulama.get("/ozel")
    def ozel():
        return PlainTextResponse("x", headers={"Referrer-Policy": "same-origin"})

    cevap = TestClient(uygulama).get("/ozel")
    assert cevap.headers["referrer-policy"] == "same-origin"
    assert cevap.headers.get_list("referrer-policy") == ["same-origin"]


def test_arayuz_her_acilista_dogrulanir_degismeyen_dosya_inmez(istemci_dbsiz):
    # Güncellemeden sonra tarayıcı eski JS'i kendi kendine kullanmasın (no-cache), ama değişmeyen
    # dosya için 304 dönsün.
    for yol in ("/", "/static/app.js", "/static/periyodik.js", "/static/style.css"):
        assert istemci_dbsiz.get(yol).headers["cache-control"] == "no-cache", yol
    ilk = istemci_dbsiz.get("/static/app.js")
    tekrar = istemci_dbsiz.get("/static/app.js", headers={"If-None-Match": ilk.headers["etag"]})
    assert tekrar.status_code == 304 and tekrar.content == b""
    assert "cache-control" not in istemci_dbsiz.get("/openapi.json").headers  # API dokunulmaz
