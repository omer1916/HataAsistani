"""Uygulama ayarları. Önce ortam değişkenlerine, yoksa proje kökündeki .env dosyasına bakılır."""

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_DOSYASI = Path(__file__).resolve().parent.parent / ".env"


class Ayarlar(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_DOSYASI, env_file_encoding="utf-8", extra="ignore"
    )

    # Varsayılan: yalnızca 127.0.0.1'e açık yerel demo veritabanı (docker-compose.yml).
    # Başka bir ortamda DATABASE_URL ortam değişkeniyle verilir.
    database_url: str = (
        "postgresql://andon:andon@127.0.0.1:5432/andon"  # secscope: ignore SECRET-DBURL-001
    )

    llm_saglayici: Literal["gemini", "ollama"] = "gemini"
    llm_model: str | None = None  # boşsa sağlayıcının varsayılan modeli
    gemini_api_key: str | None = None
    ollama_url: str = "http://localhost:11434/v1"

    # Boşsa süreç başına rastgele bir anahtar üretilir; sunucu yeniden başlayınca oturumlar düşer.
    jwt_gizli_anahtar: str | None = None
    # seed.py'nin oluşturduğu demo kullanıcılarının (operator, bakim) parolası
    demo_parola: str = "andon-demo"

    # Telegram botu (scripts/telegram_bot.py). Token @BotFather'dan alınır.
    telegram_bot_token: str | None = None
    # Botun kullanıcı adı (ör. hata_asistani_bot); verilirse arayüz tek tıkla eşleştirme
    # bağlantısı gösterir.
    telegram_bot_kullanici_adi: str | None = None
    # Arıza bildiriminin ayrıntısı. Bot mesajları Telegram sunucularından geçer; "kisa" modda
    # kılavuz alıntısı ve stok gönderilmez, yalnızca hattın durduğu bildirilir.
    telegram_bildirim_ayrintisi: Literal["ayrintili", "kisa"] = "ayrintili"
    # Telegram'dan gelen talep fotoğrafları bu kadar gün sonra silinir.
    telegram_fotograf_saklama_gun: int = Field(90, ge=1)


def ayarlar() -> Ayarlar:
    """Her çağrıda yeniden okunur; testlerde ortam değişkeni değiştirmek yeterli olsun."""
    return Ayarlar()
