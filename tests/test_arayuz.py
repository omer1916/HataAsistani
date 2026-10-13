import psycopg
import pytest

from tests.conftest import BAKIM, OPERATOR, yetki


def test_ana_sayfa_ve_statik_dosyalar_girissiz_sunulur(istemci):
    sayfa = istemci.get("/", headers={"Authorization": ""})
    assert sayfa.status_code == 200
    assert "text/html" in sayfa.headers["content-type"]
    for dosya in ["/static/app.js", "/static/style.css"]:
        assert istemci.get(dosya, headers={"Authorization": ""}).status_code == 200
    # Sekme ikonu dosyadan gelir; CSP data: adreslerine izin vermez.
    assert 'href="/static/ikon.svg"' in sayfa.text
    ikon = istemci.get("/static/ikon.svg", headers={"Authorization": ""})
    assert ikon.status_code == 200
    assert ikon.headers["content-type"].startswith("image/svg+xml")


@pytest.mark.parametrize(
    ("kullanici", "kod", "durum"),
    [
        (BAKIM, "PRES-BK-01", 200),
        (BAKIM, "PRES-OT-01", 200),
        (OPERATOR, "PRES-OT-01", 200),
        (OPERATOR, "PRES-BK-01", 404),  # yetkisiz: dokümanın varlığı da gizlenir
        (BAKIM, "YOK-01", 404),
        (BAKIM, "..%2F..%2F.env", 404),
    ],
)
def test_pdf_rol_yetkisine_uyar(istemci, kullanici, kod, durum):
    cevap = istemci.get(f"/dokumanlar/{kod}/pdf", headers=yetki(kullanici))
    assert cevap.status_code == durum
    if durum == 200:
        assert cevap.headers["content-type"] == "application/pdf"
        assert cevap.content.startswith(b"%PDF")


@pytest.mark.parametrize(
    "dosya",
    ["../../.env", "../../app/main.py", "..\\..\\.env", "/etc/hosts", "PRES-OT-01.md"],
)
def test_pdf_bozuk_dosya_adi_klasor_disina_cikamaz(istemci, test_veritabani, dosya):
    # Veritabanındaki dosya adı bozulsa bile yalnızca kılavuz klasöründeki PDF'ler sunulur.
    with psycopg.connect(test_veritabani, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO dokumanlar (kod, baslik, dosya, erisim, sayfa_sayisi) "
            "VALUES ('BOZUK-01', 'Bozuk', %s, 'operasyon', 1)",
            [dosya],
        )
        try:
            cevap = istemci.get("/dokumanlar/BOZUK-01/pdf", headers=yetki(BAKIM))
        finally:
            conn.execute("DELETE FROM dokumanlar WHERE kod = 'BOZUK-01'")
    assert cevap.status_code == 404
    assert not cevap.content.startswith(b"%PDF")


def test_pdf_girissiz_401(istemci):
    cevap = istemci.get("/dokumanlar/PRES-OT-01/pdf", headers={"Authorization": ""})
    assert cevap.status_code == 401
