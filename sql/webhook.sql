-- Dış sistemlere (ERP, MES, Teams, Slack) imzalı webhook bildirimleri. Her açılışta
-- (scripts/hazirla.py) ve her seed'de çalışır; IF NOT EXISTS sayesinde tanımlar ve gönderim
-- geçmişi korunur. Gönderici: scripts/webhook_gonderici.py, mantık: app/webhook.py.
--
-- Giden kutusu (outbox) düzeni: tetikleyici, arızayı değiştiren işlemin içinde bildirim satırını
-- yazar; işlem geri alınırsa bildirim de yoktur, işlem tamamlandıysa bildirim kaybolmaz.
-- Gönderici satırları sırayla gönderir ve sonucu aynı satıra yazar.

CREATE TABLE IF NOT EXISTS webhooklar (
    ad       text        PRIMARY KEY CHECK (ad ~ '^[a-z0-9][a-z0-9_-]{1,39}$'),
    adres    text        NOT NULL CHECK (adres ~ '^https?://[^[:space:]]+$' AND length(adres) <= 500),
    -- json: imzalı ham olay (ERP, MES, kendi servisiniz); teams ve slack: kanala okunur mesaj.
    bicim    text        NOT NULL DEFAULT 'json' CHECK (bicim IN ('json', 'teams', 'slack')),
    -- HMAC anahtarı. İmzayı üretmek için düz hâli gerekir (özeti yetmez); yalnızca webhook
    -- eklenirken bir kez gösterilir, alıcı imzayı bununla doğrular.
    gizli    text        NOT NULL CHECK (length(gizli) >= 32),
    olaylar  text[]      NOT NULL CHECK (
        cardinality(olaylar) > 0
        AND olaylar <@ ARRAY['ariza_acildi', 'ariza_kapandi', 'hat_durdu',
                             'periyodik_bakim_yapildi']::text[]),
    aktif    boolean     NOT NULL DEFAULT true,
    eklenme  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS giden_bildirimler (
    id        bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    webhook   text        NOT NULL REFERENCES webhooklar (ad) ON DELETE CASCADE,
    olay      text        NOT NULL,
    icerik    jsonb       NOT NULL,
    olusma    timestamptz NOT NULL DEFAULT now(),
    durum     text        NOT NULL DEFAULT 'bekliyor'
        CHECK (durum IN ('bekliyor', 'gonderildi', 'vazgecildi')),
    deneme    integer     NOT NULL DEFAULT 0,
    sonraki   timestamptz NOT NULL DEFAULT now(),  -- bir sonraki deneme
    son_hata  text,
    gonderim  timestamptz
);
-- Her webhook'un sıradaki bildirimi (gönderici yalnızca en eskisini gönderir: sıra korunur).
CREATE INDEX IF NOT EXISTS giden_bildirimler_bekleyen
    ON giden_bildirimler (webhook, id) WHERE durum = 'bekliyor';

-- Göndericiyi uyandırır (LISTEN giden_bildirimler). Yük boş: aynı işlemdeki sinyaller birleşir.
CREATE OR REPLACE FUNCTION giden_bildirim_sinyali() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_notify('giden_bildirimler', '');
    RETURN NULL;
END
$$;

CREATE OR REPLACE TRIGGER giden_bildirim_sinyali
    AFTER INSERT ON giden_bildirimler
    FOR EACH STATEMENT EXECUTE FUNCTION giden_bildirim_sinyali();

-- Olayı dinleyen her açık webhook için bir bildirim satırı.
CREATE OR REPLACE FUNCTION webhook_bildirimi_ekle(olay text, icerik jsonb) RETURNS void
LANGUAGE sql AS $$
    INSERT INTO giden_bildirimler (webhook, olay, icerik)
    SELECT ad, olay, jsonb_build_object('olay', olay, 'zaman', now()) || icerik
    FROM webhooklar WHERE aktif AND olay = ANY (olaylar) ORDER BY ad;
$$;

-- Arıza açıldı (ya da kapandıktan sonra yeniden açıldı), kapandı, sonradan hattı durdurdu.
-- Kapalı olarak eklenen arıza (geçmiş kayıt) "ariza_kapandi" olarak bildirilir.
CREATE OR REPLACE FUNCTION ariza_webhooku() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    olay text;
BEGIN
    IF TG_OP = 'INSERT' THEN
        olay := CASE WHEN NEW.bitis IS NULL THEN 'ariza_acildi' ELSE 'ariza_kapandi' END;
    ELSIF OLD.bitis IS NULL AND NEW.bitis IS NOT NULL THEN
        olay := 'ariza_kapandi';
    ELSIF OLD.bitis IS NOT NULL AND NEW.bitis IS NULL THEN
        olay := 'ariza_acildi';
    ELSIF NEW.bitis IS NULL AND NEW.hat_durdu AND NOT OLD.hat_durdu THEN
        olay := 'hat_durdu';
    END IF;
    IF olay IS NULL OR NOT EXISTS (SELECT 1 FROM webhooklar WHERE aktif AND olay = ANY (olaylar))
    THEN
        RETURN NULL;
    END IF;
    PERFORM webhook_bildirimi_ekle(olay, jsonb_build_object('ariza', jsonb_build_object(
        'id', NEW.id, 'makine_kodu', m.kod, 'makine', m.ad, 'hat', h.ad,
        'baslangic', NEW.baslangic, 'bitis', NEW.bitis, 'ariza_tipi', NEW.ariza_tipi,
        'onem', NEW.onem, 'hat_durdu', NEW.hat_durdu, 'aciklama', NEW.aciklama,
        'yeniden_acildi', TG_OP = 'UPDATE' AND OLD.bitis IS NOT NULL AND NEW.bitis IS NULL,
        'sure_dk', CASE WHEN NEW.bitis IS NOT NULL
                        THEN round(extract(epoch FROM NEW.bitis - NEW.baslangic) / 60, 1) END)))
    FROM makineler m JOIN hatlar h ON h.id = m.hat_id
    WHERE m.id = NEW.makine_id;
    RETURN NULL;
END
$$;

CREATE OR REPLACE TRIGGER ariza_webhooku
    AFTER INSERT OR UPDATE OF bitis, hat_durdu ON ariza_kayitlari
    FOR EACH ROW EXECUTE FUNCTION ariza_webhooku();

-- Periyodik bakım yapıldı: plana bağlı periyodik iş emri kapandı (web arayüzünde ya da botta
-- "Yapıldı").
CREATE OR REPLACE FUNCTION periyodik_webhooku() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.plan_id IS NULL OR NEW.durum <> 'kapali'
       OR (TG_OP = 'UPDATE' AND OLD.durum = 'kapali')
       OR NOT EXISTS (SELECT 1 FROM webhooklar
                      WHERE aktif AND 'periyodik_bakim_yapildi' = ANY (olaylar)) THEN
        RETURN NULL;
    END IF;
    PERFORM webhook_bildirimi_ekle('periyodik_bakim_yapildi', jsonb_build_object('bakim',
        jsonb_build_object(
            'is_emri_id', NEW.id, 'plan_id', NEW.plan_id, 'ad', p.ad, 'makine_kodu', m.kod,
            'makine', m.ad, 'hat', h.ad, 'yapan', NEW.atanan, 'tarih', NEW.kapanis,
            'aciklama', NEW.aciklama, 'aralik_gun', p.aralik_gun,
            'dokuman', CASE WHEN p.dokuman_kodu IS NOT NULL
                            THEN p.dokuman_kodu || ' s. ' || p.sayfa END)))
    FROM periyodik_bakimlar p JOIN makineler m ON m.id = p.makine_id
    JOIN hatlar h ON h.id = m.hat_id
    WHERE p.id = NEW.plan_id;
    RETURN NULL;
END
$$;

CREATE OR REPLACE TRIGGER periyodik_webhooku
    AFTER INSERT OR UPDATE OF durum ON is_emirleri
    FOR EACH ROW EXECUTE FUNCTION periyodik_webhooku();
