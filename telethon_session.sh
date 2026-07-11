# Установите telethon, если ещё не установлен
pip install telethon

# Создайте скрипт для авторизации
cat > create_session.py << 'EOF'
from telethon import TelegramClient
import asyncio

API_ID = 1111111  # Замените на ваш API_ID
API_HASH = "11111111111111111111111"  # Замените на ваш API_HASH

async def main():
    client = TelegramClient('telegram_session', API_ID, API_HASH)
    await client.start()
    print("✅ Авторизация успешна! Файл telegram_session.session создан.")
    await client.disconnect()

asyncio.run(main())
EOF

# Запустите скрипт
python3 create_session.py
