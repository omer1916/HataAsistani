"""Tarayıcıya gönderilen güvenlik başlıkları.

Content-Security-Policy, XSS'e karşı ikinci savunma hattıdır: model cevabı zaten kaçışlanıyor
(static/metin.js); bir gün bir kaçış hatası olsa bile tarayıcı sayfaya sızan bir scripti
çalıştırmaz. Arayüzde satır içi script veya stil, dışarıdan yüklenen bir kaynak yoktur; politika
bu yüzden yalnızca kendi kaynağına izin verir.

GovdeSiniri da burada: makinelerden olay alan uç noktanın istek gövdesini sınırlar.
"""

import json

CSP = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        # blob: Telegram'dan gelen talep fotoğrafları (rapor.js)
        "img-src 'self' blob:",
        "connect-src 'self'",
        # Kılavuz PDF'i token gerektirdiği için indirilip blob: adresiyle gösterilir (app.js)
        "frame-src blob:",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)

GENEL_BASLIKLAR = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=(), usb=()"),
]

# Swagger UI ve ReDoc kendi scriptlerini CDN'den ve satır içi yükler; sıkı politika onları kırar.
CSP_DISI_YOLLAR = ("/docs", "/redoc")

# Arayüz (sayfa ve /static) her açılışta sunucuya sorulsun. Cache-Control olmadan tarayıcı
# Last-Modified'a bakıp eski JS'i saatlerce kendi kendine kullanabiliyor: güncellemeden sonra
# yeni HTML eski JS ile açılıyor, yeni ekran çalışmıyordu. no-cache önbelleği kapatmaz;
# değişmeyen dosya için sunucu 304 döner, dosya yeniden inmez.
ARAYUZ_ON_EKI = "/static/"


class GuvenlikBasliklari:
    """Her HTTP yanıtına güvenlik başlıklarını ekleyen ASGI ara katmanı.

    Yanıtı tamponlamaz; yalnızca başlık mesajına dokunur. Uygulamanın kendisi aynı başlığı
    zaten koyduysa onu değiştirmez.
    """

    def __init__(self, uygulama):
        self.uygulama = uygulama

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.uygulama(scope, receive, send)
            return
        basliklar = list(GENEL_BASLIKLAR)
        if not scope["path"].startswith(CSP_DISI_YOLLAR):
            basliklar.append((b"content-security-policy", CSP.encode()))
        if scope["path"] == "/" or scope["path"].startswith(ARAYUZ_ON_EKI):
            basliklar.append((b"cache-control", b"no-cache"))

        async def gonder(mesaj):
            if mesaj["type"] == "http.response.start":
                mevcut = {ad.lower() for ad, _ in mesaj.get("headers", [])}
                eklenecek = [(ad, deger) for ad, deger in basliklar if ad not in mevcut]
                mesaj = {**mesaj, "headers": [*mesaj.get("headers", []), *eklenecek]}
            await send(mesaj)

        await self.uygulama(scope, receive, gonder)


class GovdeSiniri:
    """Belirli yollara gelen isteğin gövdesini sınırlar: aşan istek uygulamaya hiç ulaşmadan 413
    alır. Makinelerden olay alan uç nokta fabrika ağındaki cihazlara açıktır; hatalı ya da kötü
    niyetli bir cihaz devasa bir gövdeyle API'nin belleğini doldurmasın. FastAPI gövdeyi
    doğrulamadan önce bütünüyle okuduğu için sınır uygulamanın önünde olmalı."""

    def __init__(self, uygulama, onek: str = "/entegrasyon/", sinir: int = 1024 * 1024):
        self.uygulama, self.onek, self.sinir = uygulama, onek, sinir

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith(self.onek):
            await self.uygulama(scope, receive, send)
            return
        basliklar = dict(scope.get("headers", []))
        uzunluk = basliklar.get(b"content-length", b"0")
        if not uzunluk.isdigit() or int(uzunluk) > self.sinir:
            await self._reddet(send)
            return
        # Content-Length yalan olabilir (ya da hiç yoktur): gövde okunurken de sayılır.
        parcalar, toplam = [], 0
        while True:
            mesaj = await receive()
            if mesaj["type"] != "http.request":
                break
            toplam += len(mesaj.get("body", b""))
            if toplam > self.sinir:
                await self._reddet(send)
                return
            parcalar.append(mesaj.get("body", b""))
            if not mesaj.get("more_body", False):
                break
        govde = b"".join(parcalar)
        gonderildi = False

        async def tekrar_oku():
            nonlocal gonderildi
            if gonderildi:
                return await receive()  # istemci bağlantıyı kapatırsa http.disconnect
            gonderildi = True
            return {"type": "http.request", "body": govde, "more_body": False}

        await self.uygulama(scope, tekrar_oku, send)

    async def _reddet(self, send):
        govde = json.dumps({"detail": "\u0130stek g\u00f6vdesi \u00e7ok b\u00fcy\u00fck."}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(govde)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": govde})
