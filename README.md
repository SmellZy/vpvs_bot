# Monobank PDF Editor Bot

Telegram бот, який замінює персональні дані у PDF-виписках Monobank і очищає метадані файлу.

## Поля, що редагуються
- Ім'я та прізвище (латиниця)
- Дата народження
- ІПН (TIN)
- Серія/номер документа
- Ким виданий
- Дата видачі
- Адреса реєстрації
- IBAN

## Встановлення

```bash
# 1. Клонуй / скопіюй файли
cd monobank_bot

# 2. Встанови залежності
pip install -r requirements.txt

# 3. Отримай токен у @BotFather в Telegram

# 4. Запусти
BOT_TOKEN=1234567890:AAxxxx... python bot.py
```

### Docker (опціонально)
```dockerfile
FROM python:3.11-slim
RUN apt-get update && apt-get install -y fonts-liberation
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY bot.py .
ENV BOT_TOKEN=""
CMD ["python", "bot.py"]
```

```bash
docker build -t mono-bot .
docker run -e BOT_TOKEN=<твій_токен> mono-bot
```

## Як користуватись

1. `/start` — запустити бота
2. Надіслати PDF виписку Monobank
3. Покроково вводити нові дані (або `/skip` щоб залишити поле порожнім)
4. `/confirm` — отримати відредагований файл
5. `/cancel` — скасувати в будь-який момент

## Як це працює

1. **pikepdf** — видаляє оригінальні текстові streams з персональними даними (streams 10–18 першої сторінки)
2. **reportlab** — створює overlay з новими даними (шрифт Liberation Sans Bold, 9pt)
3. **pypdf** — накладає overlay поверх очищеного PDF
4. Метадані (XMP, DocInfo: Producer, Creator, Author тощо) повністю очищаються

## Примітки

- Бот заточений під формат виписок **Monobank (Universal Bank JSC)**
- Якщо Monobank оновить формат PDF, можливо знадобиться оновити `PERSONAL_STREAM_INDICES` і `FIELD_POSITIONS` у `bot.py`
- Шрифт Liberation Sans Bold має бути встановлений у системі (на Ubuntu/Debian: `apt install fonts-liberation`)
