"""Tarayıcıya gönderilen güvenlik başlıkları.

Content-Security-Policy, XSS'e karşı ikinci savunma hattıdır: model cevabı zaten kaçışlanıyor
(static/metin.js); bir gün bir kaçış hatası olsa bile tarayıcı sayfaya sızan bir scripti
çalıştırmaz. Arayüzde satır içi script veya stil, dışarıdan yüklenen bir kaynak yoktur; politika
bu yüzden yalnızca kendi kaynağına izin verir.
"""

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

        async def gonder(mesaj):
            if mesaj["type"] == "http.response.start":
                mevcut = {ad.lower() for ad, _ in mesaj.get("headers", [])}
                eklenecek = [(ad, deger) for ad, deger in basliklar if ad not in mevcut]
                mesaj = {**mesaj, "headers": [*mesaj.get("headers", []), *eklenecek]}
            await send(mesaj)

        await self.uygulama(scope, receive, gonder)
