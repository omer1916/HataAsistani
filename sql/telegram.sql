-- Telegram botu tabloları ve arıza sinyali. Her açılışta (scripts/hazirla.py) ve her seed'de
-- çalışır; IF NOT EXISTS sayesinde mevcut eşleştirmeler korunur.
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

-- Anlık arıza bildirimi: arıza eklendiğinde ya da hattı durdurduğu sonradan kaydedildiğinde botu
-- uyandırır (bot `LISTEN ariza_kayitlari` ile bekler). Sinyal neyin değiştiğini taşımaz, bot yeni
-- arızaları kendisi bulur; yükün boş olması sayesinde aynı işlemdeki sinyaller (ör. seed'in
-- yüzlerce satırı) Postgres'te tek bildirimde birleşir. Sinyal kaçarsa (bağlantı koptu) botun
-- yedek kontrolü yine yakalar. Seed tabloyu yeniden kurunca tetikleyici de silinir; bu dosya
-- seed'den sonra çalıştığı için yeniden oluşur.
CREATE OR REPLACE FUNCTION ariza_sinyali() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_notify('ariza_kayitlari', '');
    RETURN NULL;
END
$$;

CREATE OR REPLACE TRIGGER ariza_sinyali
    AFTER INSERT OR UPDATE OF hat_durdu ON ariza_kayitlari
    FOR EACH STATEMENT EXECUTE FUNCTION ariza_sinyali();
