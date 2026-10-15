// Makine bağlantıları ekranı (yalnızca bakım rolü): GET /entegrasyon/durum, 5 sn'de bir.

const $ = (id) => document.getElementById(id);
const YENILEME_MS = 5000;
// Kaynaktan bu kadar süredir olay gelmediyse "sessiz" gösterilir (bağlayıcı en az bu sıklıkta
// yazmaz; bilgi amaçlı, alarm değil).
const SESSIZ_DK = 60;

export const SONUC_ADLARI = {
  ariza_acildi: "arıza açıldı",
  ariza_eklendi: "süren arızaya eklendi",
  ariza_yeniden_acildi: "arıza yeniden açıldı",
  zaten_aktif: "zaten sürüyordu",
  ariza_kapandi: "arıza kapandı",
  alarm_kapandi: "alarm bitti, arıza sürüyor",
  aktif_alarm_yok: "süren alarm yoktu",
  gec_eslesti: "başlangıcıyla eşleşti",
};

const SAAT = new Intl.DateTimeFormat("tr-TR", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const TARIH_SAAT = new Intl.DateTimeFormat("tr-TR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });

export function onceMetni(iso, simdi = Date.now()) {
  const sn = Math.max(0, Math.round((simdi - new Date(iso).getTime()) / 1000));
  if (sn < 60) return `${sn} sn önce`;
  if (sn < 3600) return `${Math.floor(sn / 60)} dk önce`;
  if (sn < 86400) return `${Math.floor(sn / 3600)} sa önce`;
  return `${Math.floor(sn / 86400)} gün önce`;
}

export function makineEkrani({ api, eleman }) {
  let zamanlayici = null;
  let yukleniyor = false;

  function durum(metin) {
    $("makine-durum").textContent = metin ?? "";
  }

  function tablo(basliklar, satirlar) {
    const t = eleman("table", "oee-tablo makine-tablosu");
    t.append(eleman("thead"), eleman("tbody"));
    const baslik = eleman("tr");
    for (const ad of basliklar) {
      const th = eleman("th", null, ad);
      th.scope = "col";
      baslik.append(th);
    }
    t.tHead.append(baslik);
    for (const hucreler of satirlar) {
      const satir = eleman("tr");
      hucreler.forEach((h, i) => {
        const hucre = eleman(i === 0 ? "th" : "td");
        if (i === 0) hucre.scope = "row";
        hucre.append(h);
        satir.append(hucre);
      });
      t.tBodies[0].append(satir);
    }
    return t;
  }

  function isaretli(sinif, isaret, metin) {
    const etiket = eleman("span", `seviye-etiketi ${sinif}`);
    const sekil = eleman("span", "seviye-isareti", isaret);
    sekil.setAttribute("aria-hidden", "true");
    etiket.append(sekil, metin);
    return etiket;
  }

  function ozetKarti(seviye, isaret, baslik, deger, aciklama) {
    const kart = eleman("div", `kpi ${seviye}`);
    const ust = eleman("span", "kpi-baslik");
    const sekil = eleman("span", "seviye-isareti", `${isaret} `);
    sekil.setAttribute("aria-hidden", "true");
    ust.append(sekil, baslik);
    kart.append(ust, eleman("strong", "kpi-deger", String(deger)), eleman("span", "kpi-aciklama", aciklama));
    return kart;
  }

  function ciz(v) {
    $("makine-icerik").hidden = false;
    const simdi = Date.now();
    // Servis hesapları (yalnızca okuyan ERP/MES) olay göndermez; bağlı makine sayılmaz.
    const olayKaynaklari = v.kaynaklar.filter((k) => k.yetki !== "okuma");
    const aciklar = olayKaynaklari.filter((k) => k.aktif);
    const sonOlay = v.kaynaklar.map((k) => k.son_olay).filter(Boolean).sort().at(-1);
    $("makine-ozet").replaceChildren(
      ozetKarti(v.aktif_alarmlar.length ? "kirmizi" : "yesil", v.aktif_alarmlar.length ? "▼" : "●",
        "Süren alarm", v.aktif_alarmlar.length,
        v.aktif_alarmlar.length ? `${new Set(v.aktif_alarmlar.map((a) => a.makine_kodu)).size} makinede` : "Bütün makineler çalışıyor"),
      ozetKarti("yok", "◆", "Bağlı kaynak", `${aciklar.length} / ${olayKaynaklari.length}`, "Açık / toplam"),
      ozetKarti("yok", "◆", "Son olay", sonOlay ? onceMetni(sonOlay, simdi) : "–", sonOlay ? TARIH_SAAT.format(new Date(sonOlay)) : "Henüz olay gelmedi"),
    );

    $("makine-alarmlar").replaceChildren(
      v.aktif_alarmlar.length
        ? tablo(
            ["Makine", "Alarm", "Arıza", "Başladı", "Kaynak"],
            v.aktif_alarmlar.map((a) => [
              eleman("code", null, a.makine_kodu),
              isaretli("kirmizi", "▼", a.alarm_kodu),
              `#${a.ariza_id} ${a.ariza_aciklamasi}`,
              `${SAAT.format(new Date(a.baslangic))} (${onceMetni(a.baslangic, simdi)})`,
              a.kaynak,
            ]),
          )
        : eleman("p", "soluk", "Süren alarm yok."),
    );

    $("makine-kaynaklar").replaceChildren(
      v.kaynaklar.length
        ? tablo(
            ["Kaynak", "Durum", "Katalog", "Son olay", "Süren alarm"],
            v.kaynaklar.map((k) => {
              const sessiz = k.son_olay && simdi - new Date(k.son_olay).getTime() > SESSIZ_DK * 60000;
              const durumEtiketi = !k.aktif
                ? isaretli("kirmizi", "✕", "kapalı")
                : k.yetki === "okuma"
                  ? isaretli("yok", "◆", "servis hesabı · okuma")
                  : !k.son_olay
                    ? isaretli("sari", "◆", "hiç olay yok")
                    : sessiz
                      ? isaretli("sari", "◆", "sessiz")
                      : isaretli("yesil", "●", "açık");
              const ad = eleman("span");
              ad.append(eleman("code", null, k.ad), eleman("span", "kucuk soluk blok", k.aciklama));
              return [ad, durumEtiketi, k.katalog ?? "–", k.son_olay ? onceMetni(k.son_olay, simdi) : "–", String(k.aktif_alarm)];
            }),
          )
        : (() => {
            const p = eleman("p", "soluk", "Henüz bağlı makine yok. Bir PLC ya da CNC bağlamak için kaynak ekleyip anahtarını bağlayıcıya verin: ");
            p.append(eleman("code", null, "python -m scripts.entegrasyon kaynak-ekle AD AÇIKLAMA"));
            return p;
          })(),
    );

    $("makine-olaylar").replaceChildren(
      v.son_olaylar.length
        ? tablo(
            ["Zaman", "Makine", "Olay", "Sonuç", "Arıza", "Kaynak"],
            v.son_olaylar.map((o) => [
              SAAT.format(new Date(o.zaman)),
              eleman("code", null, o.makine_kodu),
              o.tur === "alarm_basladi" ? isaretli("kirmizi", "▼", `${o.alarm_kodu} başladı`) : isaretli("yesil", "▲", `${o.alarm_kodu} bitti`),
              SONUC_ADLARI[o.sonuc] ?? o.sonuc,
              o.ariza_id ? `#${o.ariza_id}` : "–",
              o.kaynak,
            ]),
          )
        : eleman("p", "soluk", "Henüz olay yok."),
    );
  }

  async function yukle() {
    if (yukleniyor) return;
    yukleniyor = true;
    try {
      const cevap = await api("/entegrasyon/durum");
      if (!cevap.ok) {
        durum(`Durum alınamadı (HTTP ${cevap.status}).`);
        return;
      }
      ciz(await cevap.json());
      durum("");
    } catch (hata) {
      if (hata.name !== "OturumBitti") durum("Sunucuya ulaşılamadı; yeniden deneniyor.");
    } finally {
      yukleniyor = false;
    }
  }

  function goster() {
    gizle();
    yukle();
    zamanlayici = setInterval(() => {
      if ($("makine-ekrani").hidden || document.hidden) return; // başka ekrandayken istek atma
      yukle();
    }, YENILEME_MS);
  }

  function gizle() {
    clearInterval(zamanlayici);
    zamanlayici = null;
  }

  return { goster, gizle };
}
