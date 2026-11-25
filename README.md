# Telegram Trigger Bot (Render + Upstash Redis)

Бот для автоматических ответов в групповых чатах с меню управления в Telegram и минималистичной веб-панелью (чёрный фон, белые кнопки, шрифт Unbounded, векторные иконки Lucide).

## Что внутри архива
- `bot.py` — полный код бота и веб-панели.
- `requirements.txt` — список зависимостей (`aiogram`, `aiohttp`, `redis`).
- `README.md` — эта инструкция.

## Пошаговый запуск на Render

### 1. Подготовьте GitHub-репозиторий
1. Создайте новый приватный репозиторий на GitHub (например, `my-tg-trigger-bot`).
2. Загрузите в него файлы `bot.py` и `requirements.txt` из этого архива.

### 2. Создайте веб-сервис на Render
1. Войдите на [render.com](https://render.com/) через GitHub.
2. Нажмите **New +** → **Web Service**.
3. Выберите ваш репозиторий и нажмите **Connect**.
4. Заполните настройки сервиса:
   - **Name:** любое имя (например, `tg-trigger-bot`)
   - **Region:** `Frankfurt (EU Central)`
   - **Runtime:** `Python 3`
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python bot.py`
   - **Instance Type:** выберите бесплатный тариф **Free** (`$0/month`).

### 3. Добавьте переменные окружения (Environment Variables)
На той же странице (или во вкладке **Environment** после создания) добавьте 4 переменные:
- `BOT_TOKEN` — ваш новый токен от `@BotFather`
- `ADMIN_ID` — ваш Telegram ID (`7831019966`)
- `REDIS_URL` — строка подключения из Upstash Redis (начинается с `rediss://default:...`)
- `WEB_PASSWORD` — пароль для входа в веб-панель

Нажмите **Deploy Web Service**. Через 1–2 минуты в логах появится сообщение `Веб-панель запущена на порту...`, а бот начнёт отвечать в Telegram.

### 4. Чтобы бот не засыпал (UptimeRobot)
Бесплатный Render усыпляет контейнер, если на сайт не заходят 15 минут. Чтобы бот работал 24/7:
1. Скопируйте адрес вашего сайта на Render (вида `https://tg-trigger-bot.onrender.com`).
2. Зарегистрируйтесь на бесплатном сервисе [uptimerobot.com](https://uptimerobot.com/).
3. Нажмите **Add New Monitor**:
   - **Monitor Type:** `HTTP(s)`
   - **URL:** `https://ваш-домен.onrender.com/ping`
   - **Monitoring Interval:** `5 minutes`
4. Сохраните монитор. Теперь UptimeRobot будет пинговать адрес `/ping` каждые 5 минут, и бот не уснёт.
