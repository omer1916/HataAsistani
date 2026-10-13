"""Hata Asistanı API.

Çalıştırmak için: uvicorn app.main:app --reload
Belgeler: http://localhost:8000/docs  (sağ üstteki "Authorize" ile giriş yapılır)
"""

import threading
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import datetime, time
from functools import partial
from pathlib import Path
from typing import Annotated

import openai
import psycopg
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, status
from fastapi import Path as FastAPIPath
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles

from app import __version__, agent, bakim_plani, rag, rapor, sorgular, telegram_bot
from app.auth import AktifKullanici, Kullanici, parola_dogrula, rol_gerekli, token_uret
from app.ayarlar import ayarlar
from app.db import TZ, havuz_olustur
from app.embedding import Embedder, TembelEmbedder, varsayilan_embedder
from app.guvenlik_basliklari import GuvenlikBasliklari
from app.llm import LLM, LLMAyarHatasi, llm_olustur
from app.models import (
    VARDIYA_SAATLERI,
    AramaIstegi,
    AramaSonucu,
    ArizaFiltresi,
    ArizaListesi,
    BakimPlani,
    BakimPlaniFiltresi,
    Hat,
    KullanimOzeti,
    OeeFiltresi,
    OeeOzeti,
    RaporIstegi,
    Saglik,
    SohbetCevabi,
    SohbetIstegi,
    TelegramDurumu,
    TelegramKodu,
    Token,
    VardiyaRaporu,
)
from app.rag import KILAVUZ_KLASORU
from app.tools import AracBaglami

STATIK_KLASOR = Path(__file__).resolve().parent / "static"

router = APIRouter()


def baglanti(request: Request) -> Iterator[psycopg.Connection]:
    with request.app.state.havuz.connection() as conn:
        yield conn


Baglanti = Annotated[psycopg.Connection, Depends(baglanti)]


def embedder_getir() -> Embedder:
    """Testler bunu sahte bir embedder'la değiştirir; model indirmeden çalışırlar."""
    return TembelEmbedder()


def llm_getir() -> LLM:
    """Testler bunu senaryolu sahte bir LLM'le değiştirir."""
    try:
        return llm_olustur(ayarlar())
    except LLMAyarHatasi as hata:
        raise HTTPException(503, str(hata)) from None


def rapor_llm_getir() -> LLM | None:
    """Rapor LLM olmadan da çıkar (yorumu kurallar yazar); ayar eksikse 503 yerine None.
    Testler bunu None ya da senaryolu sahte bir LLM'le değiştirir."""
    try:
        return llm_olustur(ayarlar())
    except LLMAyarHatasi:
        return None


def _llm_hatasi(hata: BaseException) -> HTTPException | None:
    """LLM sağlayıcısının hatasını kullanıcıya anlamlı bir HTTP hatasına çevirir."""
    if isinstance(hata, openai.RateLimitError):
        return HTTPException(429, "LLM kota sınırına ulaşıldı; biraz sonra tekrar deneyin.")
    if isinstance(hata, openai.AuthenticationError | openai.PermissionDeniedError):
        return HTTPException(
            502, "LLM anahtarı geçersiz ya da yetkisiz; .env'deki anahtarı kontrol edin."
        )
    if isinstance(hata, openai.APIConnectionError):
        return HTTPException(502, "LLM sağlayıcısına ulaşılamadı (Ollama çalışıyor mu?).")
    if isinstance(hata, openai.APIError):
        return HTTPException(502, f"LLM sağlayıcısı hata verdi: {hata}")
    return None


@router.get(
    "/saglik",
    response_model=Saglik,
    responses={503: {"model": Saglik, "description": "Veritabanına ulaşılamıyor"}},
    summary="Servis ve veritabanı ayakta mı",
)
def saglik(request: Request):
    try:
        with request.app.state.havuz.connection(timeout=2) as conn:
            conn.execute("SELECT 1")
    except psycopg.Error:
        return JSONResponse(status_code=503, content={"veritabani": False})
    return Saglik(veritabani=True)


@router.post(
    "/giris",
    response_model=Token,
    responses={401: {"description": "Kullanıcı adı veya parola hatalı"}},
    summary="Kullanıcı adı ve parolayla giriş; 8 saat geçerli bir erişim token'ı döner",
)
def giris(form: Annotated[OAuth2PasswordRequestForm, Depends()], conn: Baglanti):
    satir = sorgular.kullanici_getir(conn, form.username)
    if not parola_dogrula(form.password, satir["parola_hash"] if satir else None):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Kullanıcı adı veya parola hatalı.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    kullanici = Kullanici(satir["kullanici_adi"], satir["ad_soyad"], satir["rol"])
    return Token(
        access_token=token_uret(kullanici),
        token_type="bearer",  # nosec B106  # OAuth2 standart değeri, parola değil
        kullanici_adi=kullanici.kullanici_adi,
        ad_soyad=kullanici.ad_soyad,
        rol=kullanici.rol,
    )


@router.get("/hatlar", response_model=list[Hat], summary="Üretim hatlarını listeler")
def hatlar(conn: Baglanti, _: AktifKullanici):
    return sorgular.hatlari_getir(conn)


def _hat_dogrula(conn: psycopg.Connection, hat: str | None) -> None:
    if hata := sorgular.hat_hatasi(conn, hat):
        raise HTTPException(404, hata)


@router.get(
    "/arizalar",
    response_model=ArizaListesi,
    responses={404: {"description": "Verilen adda hat yok"}},
    summary="Arızaları hat, tarih aralığı ve tipe göre listeler",
)
def arizalar(filtre: Annotated[ArizaFiltresi, Query()], conn: Baglanti, _: AktifKullanici):
    _hat_dogrula(conn, filtre.hat)
    # Filtre modelleri fazladan alan kabul etmez (extra="forbid"); değerler sorgu
    # fonksiyonunun parametrelerine gider, bir veritabanı kaydına toplu atanmaz.
    toplam, kayitlar = sorgular.arizalari_getir(  # secscope: ignore SAST-MASSASSIGN-001
        conn, **filtre.model_dump()
    )
    return ArizaListesi(toplam=toplam, arizalar=kayitlar)


@router.get(
    "/oee",
    response_model=OeeOzeti,
    responses={404: {"description": "Verilen adda hat yok"}},
    summary="OEE ve bileşenleri: toplam, hatlara, vardiyalara ve günlere göre; duruş Pareto'su",
)
def oee(filtre: Annotated[OeeFiltresi, Query()], conn: Baglanti, _: AktifKullanici):
    _hat_dogrula(conn, filtre.hat)
    f = filtre.model_dump()
    oee_hesapla = partial(sorgular.oee_hesapla, conn, **f)  # secscope: ignore SAST-MASSASSIGN-001
    (toplam,) = oee_hesapla()
    return OeeOzeti(
        toplam=toplam,
        hatlara_gore=oee_hesapla(grup="hat"),
        vardiyalara_gore=oee_hesapla(grup="vardiya"),
        gunluk=oee_hesapla(grup="gun"),
        duruslar=sorgular.durus_pareto(conn, **f),  # secscope: ignore SAST-MASSASSIGN-001
        makineler=sorgular.durduran_makineler(conn, **f),  # secscope: ignore SAST-MASSASSIGN-001
    )


@router.get(
    "/ara",
    response_model=list[AramaSonucu],
    responses={503: {"description": "Kılavuzlar henüz yüklenmemiş"}},
    summary="Kılavuzlarda anlamsal arama; yalnızca kullanıcının rolünün görebildiği dokümanlarda",
)
def ara(
    istek: Annotated[AramaIstegi, Query()],
    conn: Baglanti,
    embedder: Annotated[Embedder, Depends(embedder_getir)],
    kullanici: AktifKullanici,
):
    try:
        return rag.dokuman_ara(conn, embedder, istek.soru, istek.k, erisim=kullanici.erisim)
    except psycopg.errors.UndefinedTable:
        raise HTTPException(
            503, "Kılavuzlar henüz yüklenmemiş; önce 'python scripts/ingest.py' çalıştırın."
        ) from None


@router.post(
    "/chat",
    response_model=SohbetCevabi,
    responses={
        429: {"description": "LLM kota sınırı"},
        502: {"description": "LLM sağlayıcısı hatası"},
        503: {"description": "LLM ayarlı değil"},
    },
    summary="Asistana soru sor; agent veritabanı ve kılavuz araçlarını kullanarak cevaplar",
)
def chat(
    istek: SohbetIstegi,
    # Bağımlılıklar parametre sırasıyla çözülür: kimlik önce doğrulanmalı. Aksi hâlde giriş
    # yapmamış biri LLM ayarının eksik olduğunu 503 hatasından öğrenebilir.
    kullanici: AktifKullanici,
    conn: Baglanti,
    embedder: Annotated[Embedder, Depends(embedder_getir)],
    llm: Annotated[LLM, Depends(llm_getir)],
):
    baglam = AracBaglami(conn=conn, embedder=embedder, kullanici=kullanici)
    try:
        sonuc = agent.sohbet(istek.soru, llm, baglam)
    except agent.SohbetHatasi as hata:
        agent.kaydet(conn, istek.soru, kullanici.kullanici_adi, llm.saglayici, hata.sonuc)
        http_hatasi = _llm_hatasi(hata.__cause__)
        if http_hatasi is None:
            raise hata.__cause__ from None
        raise http_hatasi from None
    agent.kaydet(conn, istek.soru, kullanici.kullanici_adi, llm.saglayici, sonuc)

    kaynaklar = {}
    for parca in sonuc.kaynaklar:
        kaynaklar.setdefault((parca["dokuman_kodu"], parca["sayfa"]), parca)
    return SohbetCevabi(
        cevap=sonuc.cevap,
        kaynaklar=list(kaynaklar.values()),
        arac_cagrilari=sonuc.arac_cagrilari,
        kullanim={
            "model": sonuc.model,
            "adim_sayisi": sonuc.adim_sayisi,
            "girdi_token": sonuc.girdi_token,
            "cikti_token": sonuc.cikti_token,
            "maliyet_usd": sonuc.maliyet_usd,
            "sure_ms": sonuc.sure_ms,
        },
    )


@router.get(
    "/bakim-plani",
    response_model=BakimPlani,
    responses={403: {"description": "Yalnızca bakım rolü"}},
    summary="Haftalık bakım planı: makinelerin arıza riski ve ekip saatinin en iyi dağılımı",
)
def bakim_plani_getir(
    filtre: Annotated[BakimPlaniFiltresi, Query()],
    conn: Baglanti,
    _: Annotated[Kullanici, Depends(rol_gerekli("bakim"))],
):
    return bakim_plani.bakim_plani(  # secscope: ignore SAST-MASSASSIGN-001
        conn, datetime.now(TZ), **filtre.model_dump()
    )


@router.post(
    "/rapor/vardiya",
    response_model=VardiyaRaporu,
    responses={404: {"description": "Bu vardiya için üretim kaydı yok"}},
    summary="Vardiya sonu raporu: sayılar veritabanından, yorum LLM'den (sayı denetimli)",
)
def vardiya_raporu(
    kullanici: AktifKullanici,  # kimlik önce: yetkisiz istek veritabanına ve LLM'e ulaşmasın
    conn: Baglanti,
    llm: Annotated[LLM | None, Depends(rapor_llm_getir)],
    istek: RaporIstegi | None = None,
):
    istek = istek or RaporIstegi()
    if istek.tarih is None:
        baslangic = sorgular.son_vardiya_baslangici(conn)
        if baslangic is None:
            raise HTTPException(404, "Henüz tamamlanmış bir vardiya kaydı yok.")
    else:
        baslangic = datetime.combine(istek.tarih, time(VARDIYA_SAATLERI[istek.vardiya]), TZ)
        if not sorgular.vardiya_kayitli_mi(conn, baslangic):
            aralik = sorgular.uretim_araligi(conn)
            ek = f" Kayıtlar {aralik[0]} ile {aralik[1]} arasında." if aralik else ""
            raise HTTPException(
                404,
                f"{istek.tarih} {istek.vardiya}. vardiya için kayıt yok: vardiya henüz "
                "bitmemiş ya da o gün çalışılmamış olabilir (cumartesi 3., pazar 2. ve 3. "
                f"vardiya yok).{ek}",
            )
    sonuc, yorum_sonucu = rapor.rapor_olustur(conn, llm, baslangic)
    rapor.kaydet(conn, kullanici.kullanici_adi, llm, sonuc, yorum_sonucu)
    return sonuc


@router.get(
    "/dokumanlar/{kod}/pdf",
    response_class=FileResponse,
    responses={404: {"description": "Doküman yok ya da rolün görmeye yetkili değil"}},
    summary="Kılavuzun PDF'i; arayüzdeki kaynak bağlantıları bununla açılır",
)
def dokuman_pdf(
    kod: Annotated[str, FastAPIPath(pattern=r"^[A-Za-z0-9-]{1,32}$")],
    conn: Baglanti,
    kullanici: AktifKullanici,
):
    dokuman = sorgular.dokuman_getir(conn, kod)
    # Yetkisiz rol için de 404: dokümanın var olduğu bile anlaşılmasın.
    if dokuman is None or dokuman["erisim"] not in kullanici.erisim:
        raise HTTPException(404, "Doküman bulunamadı.")
    # Dosya adı kullanıcıdan değil veritabanından gelir (ingest yazar). Yine de bir kayıt
    # bozulursa ("../.env" gibi) kılavuz klasörünün dışındaki bir dosya sunulmasın.
    klasor = KILAVUZ_KLASORU.resolve()
    yol = (klasor / dokuman["dosya"]).resolve()
    if not yol.is_relative_to(klasor) or yol.suffix.lower() != ".pdf":
        raise HTTPException(404, "Doküman bulunamadı.")
    return FileResponse(  # secscope: ignore SAST-PATH-001 (yol yukarıda klasöre sınırlandı)
        yol,
        media_type="application/pdf",
        content_disposition_type="inline",
        filename=yol.name,
    )


@router.post(
    "/telegram/kod",
    response_model=TelegramKodu,
    summary="Telegram botuyla eşleştirme için 10 dakika geçerli tek kullanımlık kod",
)
def telegram_kodu(kullanici: AktifKullanici, conn: Baglanti):
    kod = telegram_bot.kod_uret(conn, kullanici.kullanici_adi)
    bot = ayarlar().telegram_bot_kullanici_adi
    return TelegramKodu(
        kod=kod,
        gecerlilik_dk=int(telegram_bot.KOD_GECERLILIK.total_seconds() // 60),
        baglanti=f"https://t.me/{bot}?start={kod}" if bot else None,
    )


@router.get(
    "/telegram/durum",
    response_model=TelegramDurumu,
    summary="Kullanıcının Telegram bağlantısı ve botun gizlilik ayarları",
)
def telegram_durumu(kullanici: AktifKullanici, conn: Baglanti):
    a = ayarlar()
    return TelegramDurumu(
        bagli=telegram_bot.bagli_mi(conn, kullanici.kullanici_adi),
        bot_kullanici_adi=a.telegram_bot_kullanici_adi,
        bildirim_ayrintisi=a.telegram_bildirim_ayrintisi,
        fotograf_saklama_gun=a.telegram_fotograf_saklama_gun,
    )


@router.delete(
    "/telegram/baglanti",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Kullanıcının Telegram bağlantısını ve bekleyen kodlarını siler",
)
def telegram_baglantisini_sil(kullanici: AktifKullanici, conn: Baglanti):
    telegram_bot.baglantiyi_sil(conn, kullanici.kullanici_adi)


@router.get(
    "/talepler/{talep_id}/fotograf",
    response_class=Response,
    responses={
        200: {"content": {"image/jpeg": {}}},
        403: {"description": "Yalnızca bakım rolü"},
        404: {"description": "Talebin fotoğrafı yok"},
    },
    summary="Telegram'dan fotoğrafla açılan bakım talebinin fotoğrafı (yalnızca bakım rolü)",
)
def talep_fotografi(
    talep_id: Annotated[int, FastAPIPath(ge=1, le=2_147_483_647)],
    conn: Baglanti,
    _: Annotated[Kullanici, Depends(rol_gerekli("bakim"))],
):
    satir = conn.execute(
        "SELECT icerik, mime FROM talep_fotograflari WHERE talep_id = %s ORDER BY id DESC LIMIT 1",
        [talep_id],
    ).fetchone()
    if satir is None:
        raise HTTPException(404, "Bu talebin fotoğrafı yok.")
    # Kişisel veri içerebilir: tarayıcı ve ara sunucular saklamasın.
    return Response(
        bytes(satir[0]),
        media_type=satir[1],
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/", include_in_schema=False)
def arayuz():
    return FileResponse(STATIK_KLASOR / "index.html")


@router.get(
    "/kullanim",
    response_model=KullanimOzeti,
    responses={403: {"description": "Yalnızca bakım rolü"}},
    summary="Agent isteklerinin toplam token, maliyet ve süre özeti (yalnızca bakım rolü)",
)
def kullanim(conn: Baglanti, _: Annotated[Kullanici, Depends(rol_gerekli("bakim"))]):
    return sorgular.llm_kullanim_ozeti(conn)


async def _veri_hatasi(request: Request, hata: psycopg.DataError) -> JSONResponse:
    """Kullanıcı girdisi veritabanının kabul etmediği bir değer içeriyorsa (örneğin NUL
    baytı) 500 yerine 422 dön; iç hata ayrıntısı dışarı sızmasın."""
    return JSONResponse(status_code=422, content={"detail": "Girdi geçersiz karakter içeriyor."})


@asynccontextmanager
async def lifespan(uygulama: FastAPI):
    # Veritabanı kapalı olsa da uygulama açılsın; /saglik durumu 503 ile bildirir.
    havuz = havuz_olustur()
    havuz.open(wait=False)
    uygulama.state.havuz = havuz
    if uygulama.state.model_on_yukle:
        # İlk /ara isteği modelin yüklenmesini (~15 sn) beklemesin.
        threading.Thread(target=varsayilan_embedder, daemon=True).start()
    yield
    havuz.close()


def uygulama_olustur(model_on_yukle: bool = True) -> FastAPI:
    """Her çağrıda yeni bir uygulama döner; testler birbirinin bağlantı havuzuna dokunmasın.

    Testler `model_on_yukle=False` verir; embedding modeli yerine sahte embedder kullanırlar.
    """
    uygulama = FastAPI(
        title="Hata Asistanı",
        description="Fabrika verisi ve bakım kılavuzları üzerinde çalışan yapay zekâ asistanı.",
        version=__version__,
        lifespan=lifespan,
    )
    uygulama.state.model_on_yukle = model_on_yukle
    uygulama.add_exception_handler(psycopg.DataError, _veri_hatasi)
    uygulama.add_middleware(GuvenlikBasliklari)
    uygulama.include_router(router)
    uygulama.mount("/static", StaticFiles(directory=STATIK_KLASOR), name="static")
    return uygulama


app = uygulama_olustur()
