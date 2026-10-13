-- Telegram botu tabloları. Her açılışta (scripts/hazirla.py) ve her seed'de çalışır; IF NOT
-- EXISTS sayesinde mevcut eşleştirmeler korunur.
--
-- Eşleştirme kullanıcı adıyla tutulur, kullanicilar tablosuna yabancı anahtar yoktur: seed o
-- tabloyu silip yeniden kurar, eşleştirmeler ise seed'den sonra da geçerli kalmalı.

-- Web arayüzünden alınan tek kullanımlık kod. Kodun kendisi değil SHA-256 özeti saklanır.
CREATE TABLE IF NOT EXISTS telegram_kodlari (
    kod_ozeti       text        PRIMARY KEY,
    kullanici_adi   text        NOT NULL,
    son_gecerlilik  timestamptz NOT NULL
);

-- Bir Hata Asistanı kullanıcısı tek bir Telegram sohbetine, bir sohbet tek bir kullanıcıya bağlanır.
CREATE TABLE IF NOT EXISTS telegram_baglantilari (
    kullanici_adi  text        PRIMARY KEY,
    chat_id        bigint      NOT NULL UNIQUE,
    baglanma       timestamptz NOT NULL DEFAULT now()
);

-- Telegram'dan gönderilen fotoğraflı bakım taleplerinin fotoğrafı. Talebe bağlıdır; seed
-- talepleri silerken (schema.sql DROP listesi) fotoğraflar da silinir.
CREATE TABLE IF NOT EXISTS talep_fotograflari (
    id        integer     GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    talep_id  integer     NOT NULL REFERENCES bakim_talepleri (id),
    icerik    bytea       NOT NULL,
    mime      text        NOT NULL,
    eklenme   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS talep_fotograflari_talep_id_idx ON talep_fotograflari (talep_id);
