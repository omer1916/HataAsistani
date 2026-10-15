// Bakım planı ekranı (yalnızca bakım rolü): GET /bakim-plani'nin cevabını gösterir.

const SAYI = new Intl.NumberFormat("tr-TR", { maximumFractionDigits: 0 });
const ONDALIK = new Intl.NumberFormat("tr-TR", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
const YUZDE = new Intl.NumberFormat("tr-TR", { style: "percent", maximumFractionDigits: 0 });

const $ = (id) => document.getElementById(id);

export function sureMetni(saat) {
  if (saat == null) return "–";
  return saat >= 48 ? `${ONDALIK.format(saat / 24)} gün` : `${SAYI.format(saat)} sa`;
}

export function modelMetni(r) {
  if (r.model === "weibull") {
    return `Weibull β ${ONDALIK.format(r.beta)} · ${r.beta_yorumu}`;
  }
  if (r.model === "ustel") return `Sabit risk · ${r.aralik_sayisi} aralık`;
  if (r.model === "az_veri") return "Az veri";
  return "Arıza yok";
}

export function bakimEkrani({ api, eleman }) {
  const form = $("plan-formu");

  form.addEventListener("submit", (olay) => {
    olay.preventDefault();
    yukle();
  });
  form.elements.ufuk_gun.addEventListener("change", yukle);

  function durum(metin) {
    $("plan-durum").textContent = metin ?? "";
    $("plan-durum").hidden = !metin;
  }

  async function yukle() {
    if (!form.reportValidity()) return;
    const parametreler = new URLSearchParams(new FormData(form));
    const dugme = form.querySelector('button[type="submit"]');
    dugme.disabled = true;
    dugme.textContent = "Hesaplanıyor…";
    durum("Hesaplanıyor…");
    try {
      const cevap = await api(`/bakim-plani?${parametreler}`);
      const veri = await cevap.json().catch(() => ({}));
      if (!cevap.ok) {
        durum(cevap.status === 422 ? "Kapasite 1 ile 200 saat arasında olmalı." : `Plan hesaplanamadı (HTTP ${cevap.status}).`);
        return;
      }
      durum(null);
      ciz(veri);
    } catch (hata) {
      if (hata.name !== "OturumBitti") durum("Sunucuya ulaşılamadı; tekrar deneyin.");
    } finally {
      dugme.disabled = false;
      dugme.textContent = "Planı hesapla";
    }
  }

  function ozetKarti(baslik, deger, aciklama) {
    const kart = eleman("div", "kpi");
    kart.append(
      eleman("span", "kpi-baslik", baslik),
      eleman("strong", "kpi-deger", deger),
      eleman("span", "kpi-aciklama", aciklama),
    );
    return kart;
  }

  function ciz(p) {
    $("plan-icerik").hidden = false;
    const secilen = p.makineler.filter((r) => r.secildi);
    const zorunlu = p.makineler.filter((r) => r.zorunlu);
    const fark = p.onlenen_durus_dk - p.acgozlu_onlenen_durus_dk;
    $("plan-ozet").replaceChildren(
      ozetKarti("Seçilen makine", String(secilen.length), `${p.secilen_saat} / ${p.kapasite_saat} saat kullanıldı`),
      ozetKarti(
        "Geciken periyodik bakım",
        String(zorunlu.length),
        zorunlu.length
          ? `Plana önce girdi: ${p.periyodik_saat} saat${zorunlu.some((r) => !r.secildi) ? " (kapasite hepsine yetmedi)" : ""}`
          : "Geciken bakım yok",
      ),
      ozetKarti(
        "Önlenmesi beklenen duruş",
        `${SAYI.format(p.onlenen_durus_dk)} dk`,
        `Önümüzdeki ${p.ufuk_gun} günde, beklenen değer`,
      ),
      ozetKarti(
        "Açgözlü seçimle",
        `${SAYI.format(p.acgozlu_onlenen_durus_dk)} dk`,
        fark > 0.5 ? `En iyi plan ${SAYI.format(fark)} dk daha fazla önlüyor` : "Bu kapasitede en iyi planla aynı",
      ),
    );
    $("plan-tablo").replaceChildren(tablo(p.makineler));
  }

  function tablo(makineler) {
    const t = eleman("table", "oee-tablo plan-tablosu");
    t.append(eleman("thead"), eleman("tbody"));
    const baslik = eleman("tr");
    const kolonlar = ["Plan", "Makine", "Arıza (90 g)", "Model", "MTBF", "Son arızadan beri", "Arıza olasılığı", "Duruş / arıza", "Önlenebilir", "Kazanç", "Bakım"];
    for (const ad of kolonlar) {
      const th = eleman("th", null, ad);
      th.scope = "col";
      baslik.append(th);
    }
    t.tHead.append(baslik);
    for (const r of makineler) {
      const satir = eleman("tr", r.secildi ? "secili" : null);
      // Ekran okuyucu "✓" yerine "Planda" okusun; td'deki aria-label her okuyucuda okunmuyor.
      const plan = eleman("td", "plan-isareti");
      if (r.secildi) {
        const isaret = eleman("span", null, "✓");
        isaret.setAttribute("aria-hidden", "true");
        plan.append(isaret);
      }
      plan.append(eleman("span", "gorunmez", r.secildi ? "Planda" : "Planda değil"));
      const makine = eleman("th");
      makine.scope = "row";
      makine.append(eleman("code", null, r.makine_kodu), eleman("span", "soluk", ` ${r.makine_adi} · ${r.hat}`));
      if (r.su_an_arizali) {
        makine.append(" ");
        const rozet = eleman("span", "rozet arizali", "şu an arızalı");
        rozet.title = "Olasılık tamirden sonrası için; bakım tamirle birlikte yapılır";
        makine.append(rozet);
      }
      if (r.zorunlu) {
        // Geciken periyodik bakım plana önce girer; hangisi olduğu satırda yazılı.
        makine.append(eleman("span", "kucuk blok dusus", `▼ Periyodik: ${r.gecikmis_bakimlar.join(", ")}`));
      }
      const model = eleman("td", null, modelMetni(r));
      if (r.olabilirlik_orani != null) {
        model.title = `Olabilirlik oranı ${ONDALIK.format(r.olabilirlik_orani)} (3,84'ten büyükse Weibull seçilir)`;
      }
      satir.append(
        plan,
        makine,
        eleman("td", null, String(r.ariza_sayisi)),
        model,
        eleman("td", null, sureMetni(r.mtbf_saat)),
        eleman("td", null, r.su_an_arizali ? "arızada" : sureMetni(r.son_arizadan_beri_saat)),
        eleman("td", null, YUZDE.format(r.ariza_olasiligi)),
        eleman("td", null, `${SAYI.format(r.ariza_basina_durus_dk)} dk`),
        eleman("td", null, YUZDE.format(r.onlenebilirlik)),
        eleman("td", "kazanc", `${ONDALIK.format(r.kazanc_dk)} dk`),
        eleman("td", null, `${r.bakim_saat} sa`),
      );
      t.tBodies[0].append(satir);
    }
    return t;
  }

  return { goster: yukle };
}
