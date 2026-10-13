"""Tanıtım PDF'indeki ekran görüntülerini çalışan uygulamadan alır.

    python sunum/ekran_goruntuleri.py --adres http://127.0.0.1:8001

Playwright gerekir (`pip install playwright`); tarayıcı indirmez, bilgisayarda kurulu Chrome'u
kullanır. Uygulama çalışıyor, veritabanı dolu ve .env'de GEMINI_API_KEY tanımlı olmalı:
sohbet ekranındaki cevap gerçek modelden gelir. Görüntüler sunum/ekranlar/ klasörüne yazılır.
"""

import argparse
import json
import urllib.parse
import urllib.request
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

KLASOR = Path(__file__).resolve().parent / "ekranlar"
DEMO_SORUSU = "Pres 3 hattında geçen ay kaç arıza oldu, bu tip arızada ilk neye bakmalıyım?"
OLCEK = 2  # baskıda keskin görünsün


def oturum_al(adres: str, kullanici: str, parola: str) -> dict:
    veri = urllib.parse.urlencode({"username": kullanici, "password": parola}).encode()
    with urllib.request.urlopen(f"{adres}/giris", data=veri, timeout=15) as cevap:
        return json.load(cevap)


def sayfa(tarayici, oturum: dict, genislik: int, yukseklik: int) -> Page:
    baglam = tarayici.new_context(
        viewport={"width": genislik, "height": yukseklik},
        device_scale_factor=OLCEK,
        locale="tr-TR",
        color_scheme="light",
    )
    deger = json.dumps(
        {"token": oturum["access_token"], "adSoyad": oturum["ad_soyad"], "rol": oturum["rol"]}
    )
    # Giriş ekranından geçmeden oturum açık başlasın (arayüz oturumu burada saklar).
    baglam.add_init_script(f"sessionStorage.setItem('andon.oturum', {json.dumps(deger)});")
    return baglam.new_page()


def kaydet(p: Page, ad: str) -> None:
    yol = KLASOR / f"{ad}.png"
    p.screenshot(path=yol)
    print(f"  {yol.name}")


def sohbet_ve_kaynak(tarayici, adres: str, oturum: dict) -> None:
    p = sayfa(tarayici, oturum, 1280, 1040)
    p.goto(f"{adres}/#sohbet")
    p.fill("#soru", DEMO_SORUSU)
    p.click("#gonder")
    p.wait_for_selector(".mesaj.asistan:not(.bekliyor) .govde", timeout=120_000)
    p.click(".mesaj.asistan .ayrinti summary")  # hangi araçların çağrıldığı görünsün
    p.wait_for_timeout(500)
    kaydet(p, "sohbet")

    p.click(".mesaj.asistan .kaynak >> nth=0")
    p.wait_for_selector("#pdf-gorunumu[open]")
    p.wait_for_timeout(6000)  # Chrome'un PDF görüntüleyicisi sayfayı çizsin
    kaydet(p, "kaynak")
    p.context.close()


def oee(tarayici, adres: str, oturum: dict) -> None:
    p = sayfa(tarayici, oturum, 1280, 1500)
    p.goto(f"{adres}/#oee")
    p.wait_for_selector("#oee-kpi > *", timeout=60_000)
    p.wait_for_selector("#oee-trend .trend", timeout=60_000)
    p.wait_for_timeout(800)
    kaydet(p, "oee")
    p.context.close()


def rapor(tarayici, adres: str, oturum: dict) -> None:
    p = sayfa(tarayici, oturum, 1280, 1400)
    p.goto(f"{adres}/#rapor")
    p.wait_for_load_state("networkidle")
    if p.is_hidden("#rapor-icerik"):
        p.click("#rapor-dugmesi")
    p.wait_for_selector("#rapor-icerik:not([hidden])", timeout=120_000)
    p.wait_for_load_state("networkidle")
    p.wait_for_timeout(800)
    kaydet(p, "rapor")
    p.context.close()


def plan(tarayici, adres: str, oturum: dict) -> None:
    # Tablo geniş: bütün sütunlar görünsün.
    p = sayfa(tarayici, oturum, 1640, 1100)
    p.goto(f"{adres}/#plan")
    p.wait_for_load_state("networkidle")
    if p.is_hidden("#plan-icerik"):
        p.click("#plan-formu [type=submit]")
    p.wait_for_selector("#plan-icerik:not([hidden])", timeout=120_000)
    p.wait_for_timeout(800)
    kaydet(p, "plan")
    p.context.close()


def main() -> None:
    ayrac = argparse.ArgumentParser(description="Sunum ekran görüntülerini alır.")
    ayrac.add_argument("--adres", default="http://127.0.0.1:8000")
    ayrac.add_argument("--parola", default="andon-demo", help="demo kullanıcılarının parolası")
    ayrac.add_argument("--sadece", nargs="*", help="sohbet, oee, rapor, plan")
    args = ayrac.parse_args()

    KLASOR.mkdir(exist_ok=True)
    bakim = oturum_al(args.adres, "bakim", args.parola)
    adimlar = {"sohbet": sohbet_ve_kaynak, "oee": oee, "rapor": rapor, "plan": plan}
    with sync_playwright() as pw:
        tarayici = pw.chromium.launch(channel="chrome", headless=True)
        for ad, adim in adimlar.items():
            if not args.sadece or ad in args.sadece:
                print(f"{ad}...")
                adim(tarayici, args.adres, bakim)
        tarayici.close()


if __name__ == "__main__":
    main()
