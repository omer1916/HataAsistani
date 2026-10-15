-- Makine entegrasyonu: makinelerden ve başka sistemlerden gelen alarm olayları.
-- Her açılışta (scripts/hazirla.py) ve her seed'de çalışır; IF NOT EXISTS sayesinde mevcut
-- kaynaklar ve katalog korunur. İşletme verisi (olaylar, aktif alarmlar) arıza kayıtlarına
-- bağlı olduğu için schema.sql'in DROP listesindedir: seed onları da sıfırlar.
-- Mantık: app/entegrasyon.py.

-- Olay gönderen sistem: bir PLC, bir CNC, bir bağlayıcı, bir MES. Her kaynağın kendi API
-- anahtarı vardır; anahtarın kendisi değil SHA-256 özeti saklanır (anahtar 256 bit rastgele,
-- yavaş bir parola özetine gerek yok). Eşlenmemiş alarm kodu kaynağın varsayılanlarıyla
-- kaydedilir; hiçbir alarm kaybolmaz.
CREATE TABLE IF NOT EXISTS entegrasyon_kaynaklari (
    ad                       text        PRIMARY KEY CHECK (ad ~ '^[a-z0-9][a-z0-9_-]{1,39}$'),
    aciklama                 text        NOT NULL,
    katalog                  text,       -- alarm kodlarının anlamı (alarm_katalogu.katalog)
    anahtar_ozeti            text        NOT NULL UNIQUE,
    aktif                    boolean     NOT NULL DEFAULT true,
    varsayilan_tip           text        NOT NULL DEFAULT 'mekanik'
        CHECK (varsayilan_tip IN ('hidrolik', 'mekanik', 'elektrik', 'sensor', 'yazilim', 'pnomatik')),
    varsayilan_onem          text        NOT NULL DEFAULT 'orta'
        CHECK (varsayilan_onem IN ('dusuk', 'orta', 'yuksek')),
    varsayilan_hat_durdurur  boolean     NOT NULL DEFAULT true,
    olusturma                timestamptz NOT NULL DEFAULT now(),
    son_olay                 timestamptz           -- kaynaktan gelen son istek
);

-- Alarm kodlarının anlamı. Desen ya tam koddur ("A101") ya da "*" ile biten bir önektir
-- ("SV*"); tam eşleşme önektense, uzun önek kısadan önce gelir. Kılavuzda bölümü olan alarm
-- kaynağıyla (doküman, sayfa) tutulur.
CREATE TABLE IF NOT EXISTS alarm_katalogu (
    katalog       text    NOT NULL,
    desen         text    NOT NULL CHECK (desen ~ '^[A-Za-z0-9._-]{1,64}[*]?$'),
    anlam         text    NOT NULL,
    ariza_tipi    text    NOT NULL
        CHECK (ariza_tipi IN ('hidrolik', 'mekanik', 'elektrik', 'sensor', 'yazilim', 'pnomatik')),
    onem          text    NOT NULL CHECK (onem IN ('dusuk', 'orta', 'yuksek')),
    hat_durdurur  boolean NOT NULL,
    dokuman_kodu  text,
    sayfa         integer CHECK (sayfa > 0),
    PRIMARY KEY (katalog, desen)
);

-- Gelen her olay. (kaynak, olay_id) tekildir: aynı olay ikinci kez gelirse (bağlayıcı yeniden
-- denedi, ağ koptu) yeniden işlenmez, ilk sonucu döner.
CREATE TABLE IF NOT EXISTS entegrasyon_olaylari (
    id          integer     GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kaynak      text        NOT NULL REFERENCES entegrasyon_kaynaklari (ad) ON DELETE CASCADE,
    olay_id     text        NOT NULL,
    tur         text        NOT NULL CHECK (tur IN ('alarm_basladi', 'alarm_bitti')),
    makine_id   integer     NOT NULL REFERENCES makineler (id),
    alarm_kodu  text        NOT NULL,
    mesaj       text,
    zaman       timestamptz NOT NULL,           -- olayın makinedeki zamanı (düzeltilmiş)
    alinma      timestamptz NOT NULL DEFAULT now(),
    sonuc       text        NOT NULL,
    ariza_id    integer     REFERENCES ariza_kayitlari (id),
    UNIQUE (kaynak, olay_id)
);
CREATE INDEX IF NOT EXISTS entegrasyon_olaylari_alarm_idx
    ON entegrasyon_olaylari (kaynak, makine_id, alarm_kodu, tur, zaman);

-- Şu an süren alarmlar ve bağlı oldukları arıza. Aynı makinede aynı kaynaktan birden çok alarm
-- aynı arızaya bağlanır (bir duruş, birkaç alarm); son alarm bitince arıza kapanır.
CREATE TABLE IF NOT EXISTS aktif_alarmlar (
    kaynak      text        NOT NULL REFERENCES entegrasyon_kaynaklari (ad) ON DELETE CASCADE,
    makine_id   integer     NOT NULL REFERENCES makineler (id),
    alarm_kodu  text        NOT NULL,
    ariza_id    integer     NOT NULL REFERENCES ariza_kayitlari (id),
    baslangic   timestamptz NOT NULL,
    PRIMARY KEY (kaynak, makine_id, alarm_kodu)
);
CREATE INDEX IF NOT EXISTS aktif_alarmlar_ariza_idx ON aktif_alarmlar (ariza_id);
