"""Güvenlik matrisi: her rol × her korumalı uç nokta × her token türü.

Beklenen durum tek bir kuraldan türetilir: geçerli token → uç noktanın rol kuralı, geçersiz ya da
eksik token → 401. Yeni bir uç nokta eklenip korunması unutulursa buradaki bir satır kırmızıya
döner.
"""

from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.auth import ALGORITMA, TOKEN_SURESI, _gizli_anahtar, token_uret
from app.main import llm_getir
from tests.conftest import BAKIM, OPERATOR, yetki
from tests.test_agent import SenaryoluLLM, cevap_ver

ROLLER = {"operator": OPERATOR, "bakim": BAKIM}
# (yöntem, yol, gövde, rol -> geçerli token'la beklenen durum)
UC_NOKTALAR = {
    "hatlar": ("GET", "/hatlar", None, {"operator": 200, "bakim": 200}),
    "arizalar": ("GET", "/arizalar?hat=Pres%203", None, {"operator": 200, "bakim": 200}),
    "oee": ("GET", "/oee?hat=Pres%203", None, {"operator": 200, "bakim": 200}),
    "bakim-plani": ("GET", "/bakim-plani", None, {"operator": 403, "bakim": 200}),
    "periyodik": ("GET", "/periyodik-bakim", None, {"operator": 403, "bakim": 200}),
    # Olmayan plan: bakım rolü 404 alır, veritabanına bir şey yazılmaz.
    "periyodik-yapildi": (
        "POST",
        "/periyodik-bakim/999999/yapildi",
        {},
        {"operator": 403, "bakim": 404},
    ),
    "ara": ("GET", "/ara?soru=hidrolik", None, {"operator": 200, "bakim": 200}),
    "kullanim": ("GET", "/kullanim", None, {"operator": 403, "bakim": 200}),
    "pdf-operasyon": ("GET", "/dokumanlar/PRES-OT-01/pdf", None, {"operator": 200, "bakim": 200}),
    "pdf-bakim": ("GET", "/dokumanlar/PRES-BK-01/pdf", None, {"operator": 404, "bakim": 200}),
    "chat": ("POST", "/chat", {"soru": "merhaba"}, {"operator": 200, "bakim": 200}),
    "rapor": ("POST", "/rapor/vardiya", {}, {"operator": 200, "bakim": 200}),
    "telegram-kod": ("POST", "/telegram/kod", None, {"operator": 200, "bakim": 200}),
    "telegram-durum": ("GET", "/telegram/durum", None, {"operator": 200, "bakim": 200}),
    "telegram-sil": ("DELETE", "/telegram/baglanti", None, {"operator": 204, "bakim": 204}),
    "talep-fotografi": ("GET", "/talepler/999999/fotograf", None, {"operator": 403, "bakim": 404}),
}


def _imzali(govde: dict) -> str:
    return jwt.encode(govde, _gizli_anahtar(), algorithm=ALGORITMA)


def _token(tur: str, rol: str) -> str | None:
    kullanici = ROLLER[rol]
    simdi = datetime.now(UTC)
    govde = {"sub": kullanici.kullanici_adi, "rol": rol, "exp": simdi + TOKEN_SURESI}
    return {
        "gecerli": lambda: token_uret(kullanici),
        "suresi-dolmus": lambda: token_uret(kullanici, simdi - TOKEN_SURESI - timedelta(seconds=5)),
        "yanlis-anahtar": lambda: jwt.encode(govde, "x" * 40, algorithm=ALGORITMA),
        "imzasiz": lambda: jwt.encode(govde, None, algorithm="none"),
        "exp-yok": lambda: _imzali({"sub": kullanici.kullanici_adi, "rol": rol}),
        "sub-yok": lambda: _imzali({"rol": rol, "exp": simdi + TOKEN_SURESI}),
        "bilinmeyen-rol": lambda: _imzali({**govde, "rol": "yonetici"}),
        "bozuk": lambda: "bu.bir.token-degil",
        "yok": lambda: None,
    }[tur]()


TOKEN_TURLERI = [
    "gecerli",
    "suresi-dolmus",
    "yanlis-anahtar",
    "imzasiz",
    "exp-yok",
    "sub-yok",
    "bilinmeyen-rol",
    "bozuk",
    "yok",
]


@pytest.fixture(scope="module")
def sahte_llm(istemci):
    istemci.app.dependency_overrides[llm_getir] = lambda: SenaryoluLLM(cevap_ver("merhaba"))
    yield
    istemci.app.dependency_overrides.pop(llm_getir, None)


@pytest.mark.parametrize("tur", TOKEN_TURLERI)
@pytest.mark.parametrize("uc_nokta", UC_NOKTALAR)
@pytest.mark.parametrize("rol", ROLLER)
def test_yetki_matrisi(istemci, sahte_llm, rol, uc_nokta, tur):
    yontem, yol, govde, gecerli_durum = UC_NOKTALAR[uc_nokta]
    token = _token(tur, rol)
    baslik = {"Authorization": f"Bearer {token}" if token else ""}
    cevap = istemci.request(yontem, yol, json=govde, headers=baslik)
    beklenen = gecerli_durum[rol] if tur == "gecerli" else 401
    assert cevap.status_code == beklenen, cevap.text[:200]
    if beklenen == 401:
        assert cevap.headers.get("www-authenticate") == "Bearer"


@pytest.mark.parametrize(
    "baslik",
    [
        "Basic YmFraW06YW5kb24tZGVtbw==",  # yanlış şema
        "Bearer",  # boş token
        "bearer  ",
        "Token abc",
    ],
)
def test_bearer_disi_yetki_basliklari_reddedilir(istemci, baslik):
    assert istemci.get("/hatlar", headers={"Authorization": baslik}).status_code == 401


@pytest.mark.parametrize(
    "yol",
    [
        "/dokumanlar/pres-bk-01/pdf",  # küçük harfle yetki atlatma denemesi
        "/dokumanlar/PRES-BK-01%00/pdf",
        "/dokumanlar/PRES-BK-01 /pdf",
        "/dokumanlar/%2E%2E%2Fsql%2Fschema.sql/pdf",
        "/dokumanlar/..%5C..%5C.env/pdf",
    ],
)
def test_operator_pdf_yolu_oynayarak_bakim_dokumani_alamaz(istemci, yol):
    cevap = istemci.get(yol, headers=yetki(OPERATOR))
    assert cevap.status_code in (404, 422)  # asla 200 ya da 500 değil
    assert not cevap.content.startswith(b"%PDF")


NUL = "\x00"


@pytest.mark.parametrize(
    ("yontem", "yol", "parametreler", "govde"),
    [
        ("GET", "/arizalar", {"hat": f"Pres 3{NUL}"}, None),
        ("GET", "/arizalar", {"hat": NUL}, None),
        ("GET", "/ara", {"soru": f"hidrolik{NUL}basınç"}, None),
        ("GET", "/dokumanlar/PRES-OT-01%00/pdf", None, None),
        ("POST", "/chat", None, {"soru": f"merhaba{NUL}"}),
    ],
    ids=["hat-sonda", "hat-tek", "ara", "pdf", "chat"],
)
def test_nul_bayti_500_uretmez(istemci, sahte_llm, yontem, yol, parametreler, govde):
    cevap = istemci.request(yontem, yol, params=parametreler, json=govde)
    assert cevap.status_code < 500, cevap.text[:200]


@pytest.mark.parametrize("kullanici_adi", [f"bakim{NUL}", NUL, f"{NUL}operator"])
def test_giriste_nul_bayti_500_uretmez(istemci, kullanici_adi):
    cevap = istemci.post("/giris", data={"username": kullanici_adi, "password": "x"})
    assert cevap.status_code in (401, 422)
