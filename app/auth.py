"""Kimlik doğrulama (JWT) ve rol bazlı yetki.

İki rol var:
- operator: yalnızca operasyon dokümanlarını (operatör talimatı, kalite prosedürü) görür.
- bakim: bakım kılavuzları dahil bütün dokümanları görür.

Yetki arama sorgusunun içinde uygulanır (`WHERE d.erisim = ANY(...)`). Operatörün
göremeyeceği bir parça veritabanından hiç gelmez, dolayısıyla LLM'e de ulaşmaz. Erişim listesi
token'daki rolden gelir; LLM'in araç argümanlarıyla değiştirilemez.

Servis hesabı (ERP, MES panosu, BI aracı): token yerine X-API-Anahtari başlığıyla, "okuma"
yetkili bir entegrasyon kaynağının anahtarı. Bakım rolü gibi okur ama yalnızca GET: yazan,
LLM'e soran ya da silen her istek 403 alır. Olay gönderen (bağlayıcı) anahtarı burada geçmez.
"""

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

import jwt
from fastapi import Depends, HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader, OAuth2PasswordBearer
from pwdlib import PasswordHash

from app import entegrasyon
from app.ayarlar import ayarlar

Rol = Literal["operator", "bakim"]
ROL_ERISIMI: dict[str, tuple[str, ...]] = {
    "operator": ("operasyon",),
    "bakim": ("operasyon", "bakim"),
}
ROL_ADLARI = {"operator": "operatör", "bakim": "bakım mühendisi"}
TOKEN_SURESI = timedelta(hours=8)  # bir vardiya
ALGORITMA = "HS256"

_parola_hash = PasswordHash.recommended()  # argon2
# Bilinmeyen kullanıcı adında da bir doğrulama yapılır; yanıt süresinden kullanıcı adının
# var olup olmadığı anlaşılmasın.
_ZAMANLAMA_HASHI = _parola_hash.hash("zamanlama-esitleme")
_GECICI_ANAHTAR = secrets.token_urlsafe(32)


@dataclass(frozen=True)
class Kullanici:
    kullanici_adi: str
    ad_soyad: str
    rol: Rol
    servis: bool = False  # servis hesabı: yalnızca okur

    @property
    def erisim(self) -> tuple[str, ...]:
        return ROL_ERISIMI[self.rol]


def parola_hashle(parola: str) -> str:
    return _parola_hash.hash(parola)


def parola_dogrula(parola: str, parola_hash: str | None) -> bool:
    if parola_hash is None:
        _parola_hash.verify(parola, _ZAMANLAMA_HASHI)
        return False
    return _parola_hash.verify(parola, parola_hash)


def _gizli_anahtar() -> str:
    return ayarlar().jwt_gizli_anahtar or _GECICI_ANAHTAR


def token_uret(kullanici: Kullanici, simdi: datetime | None = None) -> str:
    simdi = simdi or datetime.now(UTC)
    return jwt.encode(
        {
            "sub": kullanici.kullanici_adi,
            "ad": kullanici.ad_soyad,
            "rol": kullanici.rol,
            "iat": simdi,
            "exp": simdi + TOKEN_SURESI,
        },
        _gizli_anahtar(),
        algorithm=ALGORITMA,
    )


def token_coz(token: str) -> Kullanici:
    """Token'ı doğrular; imza, süre ya da içerik geçersizse jwt.InvalidTokenError fırlatır."""
    veri = jwt.decode(
        token, _gizli_anahtar(), algorithms=[ALGORITMA], options={"require": ["exp", "sub", "rol"]}
    )
    if veri["rol"] not in ROL_ERISIMI:
        raise jwt.InvalidTokenError("bilinmeyen rol")
    return Kullanici(veri["sub"], veri.get("ad", veri["sub"]), veri["rol"])


oauth2_semasi = OAuth2PasswordBearer(tokenUrl="giris", auto_error=False)
servis_anahtari = APIKeyHeader(
    name="X-API-Anahtari",
    scheme_name="ServisAnahtari",
    auto_error=False,
    description="Servis hesabının (okuma yetkili kaynak) anahtarı; yalnızca GET istekleri",
)
OKUMA_YONTEMLERI = ("GET", "HEAD")


def _yetkisiz() -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "Oturum geçersiz ya da süresi dolmuş; yeniden giriş yapın.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def aktif_kullanici(
    request: Request,
    token: Annotated[str | None, Depends(oauth2_semasi)],
    anahtar: Annotated[str | None, Security(servis_anahtari)],
) -> Kullanici:
    if token:
        try:
            return token_coz(token)
        except jwt.InvalidTokenError:
            raise _yetkisiz() from None
    if anahtar:
        with request.app.state.havuz.connection() as conn:
            kaynak = entegrasyon.kaynak_bul(conn, anahtar)
        if kaynak is not None and kaynak.yetki == "okuma":
            if request.method not in OKUMA_YONTEMLERI:
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN, "Servis hesabı yalnızca okuyabilir (GET)."
                )
            return Kullanici(f"servis:{kaynak.ad}", f"Servis hesabı {kaynak.ad}", "bakim", True)
    raise _yetkisiz()


AktifKullanici = Annotated[Kullanici, Depends(aktif_kullanici)]


def rol_gerekli(rol: Rol):
    def kontrol(kullanici: AktifKullanici) -> Kullanici:
        if kullanici.rol != rol:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Bu işlem için yetkiniz yok.")
        return kullanici

    return kontrol
