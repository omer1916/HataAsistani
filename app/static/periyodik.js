// Periyodik bakım ekranı (yalnızca bakım rolü): GET /periyodik-bakim'in cevabını gösterir,
// "Yapıldı" ile bakımı kaydeder (POST /periyodik-bakim/{plan_id}/yapildi).

const $ = (id) => document.getElementById(id);

// Durum hiçbir yerde yalnızca renkle verilmez (bkz. oee.js SEVIYE_ISARETLERI): şekil ve ad.
export const DURUMLAR = {
  gecikti: ["kirmizi", "▼", "Gecikti"],
  yaklasiyor: ["sari", "◆", "Yaklaşıyor"],
  zamaninda: ["yesil", "●", "Zamanında"],
};

// API tarihleri "YYYY-AA-GG" verir; Date'e çevirmeden yazılır (saat dilimi kaydırmasın).
export function tarihMetni(iso) {
  const [yil, ay, gun] = iso.split("-");
  return `${gun}.${ay}.${yil}`;
}

export function kalanMetni(kalan) {
  if (kalan < 0) return `${-kalan} gün gecikti`;
  if (kalan === 0) return "bugün";
  if (kalan === 1) return "yarın";
  return `${kalan} gün sonra`;
}

export function periyodikEkrani({ api, eleman, pdfAc }) {
  const filtre = $("periyodik-filtre");
  const pencere = $("yapildi-penceresi");
  const form = $("yapildi-formu");
  let veri = null;
  let secilen = null; // "Yapıldı" penceresindeki bakım

  filtre.addEventListener("change", ciz);
  filtre.addEventListener("submit", (olay) => olay.preventDefault());
  form.addEventListener("submit", kaydet);

  function durum(metin) {
    $("periyodik-durum").textContent = metin ?? "";
    $("periyodik-durum").hidden = !metin;
  }

  function hata(metin) {
    $("yapildi-hata").textContent = metin ?? "";
    $("yapildi-hata").hidden = !metin;
  }

  async function yukle(mesaj = null) {
    if (!veri) durum("Yükleniyor…");
    try {
      const cevap = await api("/periyodik-bakim");
      if (!cevap.ok) {
        durum(`Bakımlar alınamadı (HTTP ${cevap.status}).`);
        return;
      }
      veri = await cevap.json();
      durum(mesaj);
      hatSecenekleri();
      ciz();
    } catch (e) {
      if (e.name !== "OturumBitti") durum("Sunucuya ulaşılamadı; tekrar deneyin.");
    }
  }

  function hatSecenekleri() {
    const secim = filtre.elements.hat;
    const onceki = secim.value;
    const hatlar = [...new Set(veri.bakimlar.map((b) => b.hat))].sort((a, b) =>
      a.localeCompare(b, "tr"),
    );
    secim.replaceChildren(new Option("Bütün hatlar", ""), ...hatlar.map((h) => new Option(h, h)));
    secim.value = hatlar.includes(onceki) ? onceki : "";
  }

  function ozetKarti(durumAdi, baslik, sayi, aciklama) {
    const [seviye, isaret] = DURUMLAR[durumAdi];
    const kart = eleman("div", `kpi ${seviye}`);
    const baslikAlani = eleman("span", "kpi-baslik");
    const sekil = eleman("span", "seviye-isareti", `${isaret} `);
    sekil.setAttribute("aria-hidden", "true");
    baslikAlani.append(sekil, baslik);
    kart.append(
      baslikAlani,
      eleman("strong", "kpi-deger", String(sayi)),
      eleman("span", "kpi-aciklama", aciklama),
    );
    return kart;
  }

  function durumEtiketi(b) {
    const [seviye, isaret, ad] = DURUMLAR[b.durum];
    const etiket = eleman("span", `seviye-etiketi ${seviye}`);
    const sekil = eleman("span", "seviye-isareti", isaret); // renk şekilde, yazı metin renginde
    sekil.setAttribute("aria-hidden", "true");
    etiket.append(sekil, ad);
    return etiket;
  }

  function ciz() {
    if (!veri) return;
    $("periyodik-icerik").hidden = false;
    const { hat, durum: istenen } = Object.fromEntries(new FormData(filtre));
    const gorunen = veri.bakimlar.filter(
      (b) => (!hat || b.hat === hat) && (!istenen || b.durum === istenen),
    );
    const zamaninda = veri.bakimlar.length - veri.gecikmis - veri.yaklasan;
    $("periyodik-ozet").replaceChildren(
      ozetKarti("gecikti", "Geciken", veri.gecikmis, "Sıradaki tarihi geçmiş"),
      ozetKarti("yaklasiyor", `Önümüzdeki ${veri.yaklasma_gun} gün`, veri.yaklasan, "Bugün dahil"),
      ozetKarti("zamaninda", "Zamanında", zamaninda, `${veri.bakimlar.length} bakım planından`),
    );
    $("periyodik-tablo").replaceChildren(
      gorunen.length ? tablo(gorunen) : eleman("p", "soluk", "Bu filtreye uyan bakım yok."),
    );
  }

  function tablo(bakimlar) {
    const t = eleman("table", "oee-tablo periyodik-tablosu");
    t.append(eleman("thead"), eleman("tbody"));
    const baslik = eleman("tr");
    for (const ad of ["Durum", "Makine", "Bakım", "Son bakım", "Sıradaki", "Kaynak"]) {
      const th = eleman("th", null, ad);
      th.scope = "col";
      baslik.append(th);
    }
    const islemBasligi = eleman("th");
    islemBasligi.scope = "col";
    islemBasligi.append(eleman("span", "gorunmez", "İşlem"));
    baslik.append(islemBasligi);
    t.tHead.append(baslik);

    for (const b of bakimlar) {
      const satir = eleman("tr", b.durum === "gecikti" ? "gecikmis" : null);
      const durumHucresi = eleman("td");
      durumHucresi.append(durumEtiketi(b), eleman("span", "kucuk soluk blok", kalanMetni(b.kalan_gun)));

      const makine = eleman("th");
      makine.scope = "row";
      makine.append(
        eleman("code", null, b.makine_kodu),
        ` ${b.makine_adi}`,
        eleman("span", "kucuk soluk blok", b.hat),
      );

      const bakim = eleman("td");
      bakim.append(
        eleman("strong", null, b.bakim),
        eleman("span", "soluk", ` · ${b.aralik_gun} günde bir`),
        eleman("span", "kucuk soluk blok yapilacaklar", b.yapilacaklar),
      );

      const son = eleman("td");
      son.append(
        tarihMetni(b.son_bakim),
        eleman("span", "kucuk soluk blok", b.kayitli ? (b.son_yapan ?? "") : "sistem öncesi kayıt"),
      );
      if (b.acik_is_emri) son.append(eleman("span", "rozet", "iş emri açık"));

      const kaynak = eleman("td");
      if (b.dokuman_kodu) {
        const dugme = eleman("button", "metin-dugme kaynak-dugmesi", `${b.dokuman_kodu} s. ${b.sayfa}`);
        dugme.type = "button";
        dugme.addEventListener("click", () => pdfAc(b.dokuman_kodu, b.sayfa));
        kaynak.append(dugme);
        if (b.bolum) kaynak.append(eleman("span", "kucuk soluk blok bolum", b.bolum));
      } else {
        kaynak.append(eleman("span", "soluk", "kılavuzda takvim yok"));
      }

      const islem = eleman("td");
      const yapildi = eleman("button", "ana-dugme kucuk-dugme", "Yapıldı");
      yapildi.type = "button";
      yapildi.setAttribute("aria-label", `${b.makine_kodu} ${b.bakim}: yapıldı olarak kaydet`);
      yapildi.addEventListener("click", () => ac(b));
      islem.append(yapildi);

      satir.append(durumHucresi, makine, bakim, son, eleman("td", null, tarihMetni(b.siradaki)), kaynak, islem);
      t.tBodies[0].append(satir);
    }
    return t;
  }

  function ac(b) {
    secilen = b;
    $("yapildi-plan").textContent = `${b.makine_kodu} ${b.makine_adi} · ${b.bakim}`;
    $("yapildi-yapilacaklar").textContent = b.yapilacaklar;
    form.reset();
    form.elements.tarih.value = veri.bugun;
    form.elements.tarih.max = veri.bugun; // ileri tarihli bakım kaydedilmez
    form.elements.sure_saat.value = String(b.sure_saat);
    hata(null);
    pencere.showModal();
  }

  async function kaydet(olay) {
    olay.preventDefault();
    if (!secilen || !form.reportValidity()) return;
    const dugme = form.querySelector('button[type="submit"]');
    dugme.disabled = true; // çift tıklama iki iş emri açmasın
    dugme.textContent = "Kaydediliyor…";
    const alanlar = new FormData(form);
    const govde = { tarih: alanlar.get("tarih"), sure_saat: Number(alanlar.get("sure_saat")) };
    const not = String(alanlar.get("aciklama") ?? "").trim();
    if (not) govde.aciklama = not;
    try {
      const cevap = await api(`/periyodik-bakim/${secilen.plan_id}/yapildi`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(govde),
      });
      const sonuc = await cevap.json().catch(() => ({}));
      if (!cevap.ok) {
        hata(typeof sonuc.detail === "string" ? sonuc.detail : "Kaydedilemedi; tarihi ve süreyi kontrol edin.");
        return;
      }
      pencere.close();
      await yukle(`${sonuc.makine_kodu} ${sonuc.bakim} kaydedildi. Sıradaki: ${tarihMetni(sonuc.siradaki)}.`);
      // Tablo yeniden çizildiği için tıklanan düğme yok; odak kaybolmasın, sonuca gitsin.
      $("periyodik-durum").focus();
    } catch (e) {
      if (e.name !== "OturumBitti") hata("Sunucuya ulaşılamadı; tekrar deneyin.");
    } finally {
      dugme.disabled = false;
      dugme.textContent = "Kaydet";
    }
  }

  return { goster: () => yukle() };
}
