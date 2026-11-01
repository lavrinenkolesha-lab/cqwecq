#!/usr/bin/env python3
"""
Cerberus Games Market Bot - Ultra-Fast HTTP/2 Edition
=====================================================
Высокоскоростной Telegram-снайпер и монитор аукциона cerberus-games.com/market.

Ключевые оптимизации:
1. HTTP/2 Мультиплексирование (httpx + h2): постоянный защищенный туннель без повторных TLS-рукопожатий.
   Время ответа сервера сокращено до ~350-450 мс.
2. Частота опроса ~1 секунда на полный цикл: пауза 0.4 сек между запросами.
3. Защита от Tarpit-замедлений ServicePipe: автоматический замер времени ответа и плавный backoff.
4. Ультрабыстрый Fast-Check (0.1 мс): regex-проверка ID верхнего лота без тяжелого парсинга HTML.
5. Фильтры: вещи Тип C только >= 40 ур., выгода строго от 500 CS, вишлист и отправка в группу/ЛС.
"""

import os
import sys
import time
import json
import random
import logging
import threading
import re
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Dict, Any, List, Optional, Tuple

try:
    import httpx
    from bs4 import BeautifulSoup
except ImportError:
    print("Run: pip install \"httpx[http2]\" beautifulsoup4")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("CerberusSpeedBot")

BOT_CONFIG_FILE = "tg_bot_config.json"

DEFAULT_TG_CONFIG = {
    "telegram": {
        "bot_token": "YOUR_BOT_TOKEN_FROM_BOTFATHER",
        "admin_chat_id": 0,
        "group_chat_id": 0
    },
    "cerberus": {
        "base_url": "https://cerberus-games.com",
        "market_path": "/market",
        "buy_endpoint": "/market/buy",
        "cookies": {
            "xf_user": "",
            "xf_session": "",
            "cerberus_session": ""
        },
        "headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": "https://cerberus-games.com/market"
        }
    },
    "strategy": {
        "auto_buy": False,
        "poll_interval_sec": 0.4,
        "min_discount_pct": 20.0,
        "min_profit_cs": 500,
        "min_type_c_level": 40,
        "max_price_per_lot": 50000,
        "tarpit_backoff_sec": 1.8,
        "wishlist": [
            {"name": "Талик Невежества", "max_price": 180},
            {"name": "Талик Покровительства", "max_price": 140},
            {"name": "Руда Т5", "max_price": 15}
        ]
    }
}


class HealthCheckHandler(BaseHTTPRequestHandler):
    """Легковесный HTTP-сервер для поддержки Render/Bothost."""
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Cerberus Ultra-Fast Bot is running!")

    def log_message(self, format, *args):
        pass


def start_http_health_server():
    port = int(os.environ.get("PORT", 8080))
    try:
        server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
        logger.info(f"Health check сервер слушает порт {port}")
        server.serve_forever()
    except Exception as e:
        logger.warning(f"Не удалось привязать HTTP-сервер к порту {port}: {e}")


class FastTelegramMarketBot:
    def __init__(self, config_path: str = BOT_CONFIG_FILE):
        self.config_path = config_path
        self.config = self._load_config()
        self.token = self.config["telegram"]["bot_token"]
        self.api_url = f"https://api.telegram.org/bot{self.token}"
        self.last_update_id = 0

        # Инициализация HTTP/2 клиента с сохранением пула соединений
        self.client = self._init_http2_client()

        self.running = True
        self.stats = {
            "fast_checks": 0,
            "full_parses": 0,
            "deals_found": 0,
            "purchased": 0,
            "total_spent": 0,
            "tarpit_detections": 0,
            "avg_latency_ms": 0.0
        }
        self.last_top_lot_id: Optional[str] = None
        self.last_html_hash: Optional[int] = None
        self.seen_lots = set()

    def _load_config(self) -> Dict[str, Any]:
        if not os.path.exists(self.config_path):
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_TG_CONFIG, f, indent=2, ensure_ascii=False)
            return DEFAULT_TG_CONFIG
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return DEFAULT_TG_CONFIG

    def _save_config(self):
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Ошибка сохранения конфига: {e}")

    def _init_http2_client(self) -> httpx.Client:
        c_conf = self.config.get("cerberus", {})
        headers = c_conf.get("headers", {})
        cookies = {k: v for k, v in c_conf.get("cookies", {}).items() if v}

        limits = httpx.Limits(max_keepalive_connections=15, max_connections=30, keepalive_expiry=60.0)
        timeout = httpx.Timeout(connect=5.0, read=5.0, write=5.0, pool=10.0)

        return httpx.Client(
            http2=True,
            limits=limits,
            timeout=timeout,
            headers=headers,
            cookies=cookies,
            follow_redirects=True
        )

    # --- Telegram API Helpers ---
    def send_message(self, chat_id: int, text: str, reply_markup: Optional[Dict] = None) -> bool:
        if not chat_id:
            return False
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            r = self.client.post(f"{self.api_url}/sendMessage", json=payload, timeout=5.0)
            return r.status_code == 200
        except Exception as e:
            logger.error(f"Ошибка отправки сообщения в TG: {e}")
            return False

    def answer_callback(self, callback_query_id: str, text: str = ""):
        try:
            self.client.post(f"{self.api_url}/answerCallbackQuery", json={
                "callback_query_id": callback_query_id,
                "text": text
            }, timeout=3.0)
        except Exception as e:
            logger.debug(f"Ошибка ответа на callback: {e}")

    def get_main_keyboard(self) -> Dict[str, Any]:
        auto_buy_status = "🟢 ВКЛ" if self.config["strategy"]["auto_buy"] else "🔴 ВЫКЛ"
        return {
            "keyboard": [
                [{"text": "🔍 Сканировать рынок"}, {"text": f"⚡ Авто-покупка: {auto_buy_status}"}],
                [{"text": "📋 Мой вишлист"}, {"text": "⚙️ Настройки"}],
                [{"text": "📊 Статистика бота"}]
            ],
            "resize_keyboard": True
        }

    # --- Оптимизированный Fast-Check и парсинг ---
    def fetch_market_html(self) -> Tuple[Optional[str], float, int]:
        """Запрашивает страницу по HTTP/2 и замеряет точное время ответа."""
        c_conf = self.config.get("cerberus", {})
        url = c_conf.get("base_url", "https://cerberus-games.com") + c_conf.get("market_path", "/market")

        t0 = time.perf_counter()
        try:
            resp = self.client.get(url)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            return resp.text, elapsed_ms, resp.status_code
        except Exception as e:
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            logger.error(f"HTTP/2 запрос завершился ошибкой ({elapsed_ms:.1f} мс): {e}")
            return None, elapsed_ms, 0

    def fast_check_changed(self, html: str) -> Tuple[bool, Optional[str]]:
        """
        Ультрабыстрый Fast-Check (0.1 мс):
        Находит ID первого лота в HTML регулярным выражением без построения DOM-дерева BeautifulSoup.
        """
        # Ищем data-lot-id или параметр ?s= в ссылках лотов
        match = re.search(r'(?:data-lot-id=["\']|details\?s=)(\d+)', html)
        top_lot_id = match.group(1) if match else None

        current_hash = hash(html[:4000])  # Хэш шапки страницы лотов

        is_changed = False
        if top_lot_id != self.last_top_lot_id:
            is_changed = True
        elif current_hash != self.last_html_hash:
            is_changed = True

        self.last_top_lot_id = top_lot_id
        self.last_html_hash = current_hash
        return is_changed, top_lot_id

    def _parse_html_lots(self, html: str) -> List[Dict[str, Any]]:
        """Детальный парсинг лотов (вызывается только при обнаружении изменений)."""
        soup = BeautifulSoup(html, "html.parser")
        rows = soup.select("tr.market-item, tr.lot, .market-table tbody tr, .market-grid .market-item")
        if not rows:
            rows = soup.select("table tbody tr")

        lots = []
        for r in rows:
            try:
                lot_id = r.get("data-lot-id") or r.get("data-id")
                details = r.select_one("a[href*='details'], a[href*='?s=']")
                if not lot_id and details:
                    href = details.get("href", "")
                    if "?s=" in href:
                        lot_id = href.split("?s=")[-1].split("&")[0]

                if not lot_id:
                    continue

                name_el = r.select_one(".item-name, .title, .market-title, td.name, a.item-link")
                price_el = r.select_one(".price, .market-price, .cost, td.price")
                amount_el = r.select_one(".amount, .count, .quantity, td.amount")

                if not name_el or not price_el:
                    continue

                name = name_el.get_text(strip=True)
                raw_price = "".join(ch for ch in price_el.get_text(strip=True) if ch.isdigit() or ch == ".")
                price = float(raw_price) if raw_price else 0.0

                amount = 1
                if amount_el:
                    raw_amount = "".join(ch for ch in amount_el.get_text(strip=True) if ch.isdigit())
                    if raw_amount:
                        amount = max(1, int(raw_amount))

                # Извлечение уровня предмета для фильтра Type C
                level = 1
                lvl_match = re.search(r'(?:уровень|ур\.|lvl|level)[:\s]*(\d+)', r.get_text(strip=True), re.I)
                if not lvl_match:
                    lvl_match = re.search(r'\b(\d{2})\b', name)
                if lvl_match:
                    level = int(lvl_match.group(1))

                is_type_c = any(tag in name.lower() for tag in ["тип с", "тип-с", "type c", "type-c", "[c]"])

                if price > 0:
                    lots.append({
                        "lot_id": str(lot_id),
                        "name": name,
                        "amount": amount,
                        "price": price,
                        "unit_price": price / amount,
                        "level": level,
                        "is_type_c": is_type_c
                    })
            except Exception:
                continue
        return lots

    def evaluate_market_deals(self, lots: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        deals = []
        strat = self.config["strategy"]
        min_disc = strat.get("min_discount_pct", 20.0)
        min_profit_cs = strat.get("min_profit_cs", 500)
        min_type_c_lvl = strat.get("min_type_c_level", 40)
        max_lot_price = strat.get("max_price_per_lot", 50000)
        wishlist = strat.get("wishlist", [])

        # Фильтр 1: отсекаем вещи Тип C ниже 40 уровня
        filtered_lots = []
        for l in lots:
            if l["is_type_c"] and l["level"] < min_type_c_lvl:
                continue
            filtered_lots.append(l)

        # Группировка для поиска относительной выгоды
        groups = {}
        for l in filtered_lots:
            k = l["name"].lower()
            if k not in groups:
                groups[k] = []
            groups[k].append(l)

        for k, group in groups.items():
            if len(group) > 1:
                group.sort(key=lambda x: x["unit_price"])
                best = group[0]
                second = group[1]

                discount = ((second["unit_price"] - best["unit_price"]) / second["unit_price"]) * 100
                potential_profit = (second["unit_price"] * 0.95 - best["unit_price"]) * best["amount"]

                # Фильтр 2: Выгода строго от 500 CS и скидка >= min_disc
                if discount >= min_disc and potential_profit >= min_profit_cs and best["price"] <= max_lot_price:
                    best["reason"] = f"Скидка {discount:.1f}% (+{potential_profit:.0f} CS к следующему)"
                    deals.append(best)

            # Проверка целевого вишлиста
            for l in group:
                for w in wishlist:
                    if w["name"].lower() in l["name"].lower():
                        if l["unit_price"] <= w["max_price"] and l["price"] <= max_lot_price:
                            if l not in deals:
                                l["reason"] = f"Вишлист (цена {l['unit_price']:.1f} <= {w['max_price']} CS)"
                                deals.append(l)

        return deals

    def execute_buy(self, lot_id: str) -> bool:
        c_conf = self.config.get("cerberus", {})
        url = c_conf.get("base_url", "https://cerberus-games.com") + c_conf.get("buy_endpoint", "/market/buy")
        payload = {"lot_id": lot_id, "action": "buy"}

        try:
            r = self.client.post(url, data=payload, timeout=4.0)
            return r.status_code == 200 and ("success" in r.text.lower() or "куплен" in r.text.lower())
        except Exception as e:
            logger.error(f"Ошибка покупки лота #{lot_id}: {e}")
            return False

    # --- Высокоскоростной цикл сканирования с HTTP/2 и защитой Tarpit ---
    def background_speed_scanner(self):
        logger.info("⚡ Высокоскоростной сканер HTTP/2 запущен (~1 сек цикл).")
        strat = self.config["strategy"]

        while self.running:
            poll_interval = strat.get("poll_interval_sec", 0.4)
            tarpit_backoff = strat.get("tarpit_backoff_sec", 1.8)

            html, elapsed_ms, status = self.fetch_market_html()
            self.stats["fast_checks"] += 1

            # Скользящее среднее времени ответа
            self.stats["avg_latency_ms"] = round(
                (self.stats["avg_latency_ms"] * 0.9) + (elapsed_ms * 0.1), 1
            )

            # 3. Защита от Tarpit-замедлений ServicePipe:
            # Если время ответа > 1800 мс или код 429 — защита начинает включать tarpit-задержку
            if elapsed_ms > 1800 or status == 429:
                self.stats["tarpit_detections"] += 1
                logger.warning(
                    f"⚠️ Обнаружен Tarpit/Замедление ({elapsed_ms:.0f} мс, статус {status}). "
                    f"Включаем адаптивную паузу {tarpit_backoff} сек..."
                )
                time.sleep(tarpit_backoff)
                continue

            if not html or status != 200:
                time.sleep(1.0)
                continue

            # 4. Ультрабыстрый Fast-Check (0.1 мс):
            changed, top_id = self.fast_check_changed(html)
            if not changed:
                # Новых лотов нет — моментально переходим к следующему циклу
                time.sleep(poll_interval)
                continue

            # Если рынок изменился — парсим лоты
            self.stats["full_parses"] += 1
            lots = self._parse_html_lots(html)
            deals = self.evaluate_market_deals(lots)

            for deal in deals:
                lid = deal["lot_id"]
                if lid in self.seen_lots:
                    continue
                self.seen_lots.add(lid)
                self.stats["deals_found"] += 1

                admin_id = self.config["telegram"].get("group_chat_id") or self.config["telegram"].get("admin_chat_id")
                if not admin_id:
                    continue

                if strat.get("auto_buy", False):
                    success = self.execute_buy(lid)
                    if success:
                        self.stats["purchased"] += 1
                        self.stats["total_spent"] += deal["price"]
                        msg = (
                            f"⚡ <b>[АВТОВЫКУП] Лот #{lid} куплен!</b>\n"
                            f"Предмет: <b>{deal['name']}</b> (x{deal['amount']})\n"
                            f"Цена: <b>{deal['price']} CS</b>\n"
                            f"Причина: <i>{deal['reason']}</i>"
                        )
                    else:
                        msg = (
                            f"⚠️ <b>[ОШИБКА АВТОВЫКУПА] Лот #{lid}</b>\n"
                            f"Предмет: {deal['name']} за {deal['price']} CS\n"
                            f"Возможно, лот уже перекупили или истекли cookies."
                        )
                    self.send_message(admin_id, msg)
                else:
                    inline_kb = {
                        "inline_keyboard": [
                            [{"text": f"🛒 Купить #{lid} ({deal['price']} CS)", "callback_data": f"buy_{lid}"}],
                            [{"text": "🔗 Открыть на сайте", "url": f"https://cerberus-games.com/market/details?s={lid}"}]
                        ]
                    }
                    msg = (
                        f"🔥 <b>Найден выгодный лот!</b>\n"
                        f"Предмет: <b>{deal['name']}</b> (x{deal['amount']})\n"
                        f"Цена: <b>{deal['price']} CS</b> (за ед: {deal['unit_price']:.1f})\n"
                        f"Выгода: <i>{deal['reason']}</i>"
                    )
                    self.send_message(admin_id, msg, reply_markup=inline_kb)

            time.sleep(poll_interval)

    # --- Обработка команд в Telegram ---
    def process_single_update(self, update: Dict[str, Any]):
        if "callback_query" in update:
            cb = update["callback_query"]
            cb_id = cb["id"]
            data = cb.get("data", "")
            chat_id = cb["message"]["chat"]["id"]

            if data.startswith("buy_"):
                lot_id = data.replace("buy_", "")
                self.answer_callback(cb_id, f"Покупаем лот #{lot_id}...")
                success = self.execute_buy(lot_id)
                if success:
                    self.send_message(chat_id, f"🎉 <b>Лот #{lot_id} успешно куплен!</b>")
                else:
                    self.send_message(chat_id, f"❌ <b>Не удалось купить лот #{lot_id}.</b> Проверьте куки или баланс.")
            return

        msg = update.get("message")
        if not msg:
            return

        chat_id = msg["chat"]["id"]
        text = msg.get("text", "").strip()

        current_admin = self.config["telegram"].get("admin_chat_id")
        if not current_admin:
            self.config["telegram"]["admin_chat_id"] = chat_id
            self._save_config()
            self.send_message(chat_id, "👑 <b>Вы успешно авторизованы как администратор бота!</b>", reply_markup=self.get_main_keyboard())
            return

        if chat_id != current_admin and chat_id != self.config["telegram"].get("group_chat_id"):
            self.send_message(chat_id, "⛔ Доступ ограничен. Этот бот привязан к другому администратору.")
            return

        if text in ("/start", "/menu"):
            self.send_message(chat_id, "🤖 <b>Панель управления Cerberus Ultra-Fast Sniper (HTTP/2)</b>", reply_markup=self.get_main_keyboard())

        elif text == "🔍 Сканировать рынок":
            self.send_message(chat_id, "⏳ <i>Мгновенный опрос аукциона через HTTP/2...</i>")
            html, elapsed_ms, status = self.fetch_market_html()
            if not html:
                self.send_message(chat_id, f"❌ Ошибка соединения (время ответа: {elapsed_ms:.0f} мс)")
                return

            lots = self._parse_html_lots(html)
            deals = self.evaluate_market_deals(lots)
            res_text = (
                f"📡 <b>Результат HTTP/2 сканирования:</b>\n"
                f"• Время ответа сервера: <b>{elapsed_ms:.1f} мс</b>\n"
                f"• Всего активных лотов: <b>{len(lots)}</b>\n"
                f"• Выгодных предложений (выгода $\\ge$ {self.config['strategy'].get('min_profit_cs')} CS): <b>{len(deals)}</b>"
            )
            self.send_message(chat_id, res_text)

            for d in deals[:5]:
                lid = d["lot_id"]
                inline_kb = {
                    "inline_keyboard": [
                        [{"text": f"🛒 Купить за {d['price']} CS", "callback_data": f"buy_{lid}"}]
                    ]
                }
                card = (
                    f"🔹 <b>{d['name']}</b> (x{d['amount']})\n"
                    f"Цена: <b>{d['price']} CS</b> (за ед: {d['unit_price']:.1f})\n"
                    f"Выгода: <i>{d['reason']}</i>"
                )
                self.send_message(chat_id, card, reply_markup=inline_kb)

        elif "Авто-покупка" in text:
            new_val = not self.config["strategy"]["auto_buy"]
            self.config["strategy"]["auto_buy"] = new_val
            self._save_config()
            status_text = "🟢 <b>ВКЛЮЧЕНА</b>" if new_val else "🔴 <b>ВЫКЛЮЧЕНА</b>"
            self.send_message(chat_id, f"Авто-покупка теперь: {status_text}", reply_markup=self.get_main_keyboard())

        elif text == "📋 Мой вишлист":
            w_list = self.config["strategy"].get("wishlist", [])
            lines = [f"• <b>{item['name']}</b>: до {item['max_price']} CS" for item in w_list]
            msg_text = "📋 <b>Ваш вишлист приоритетных предметов:</b>\n\n" + "\n".join(lines)
            msg_text += "\n\n<i>Чтобы добавить предмет, отправьте:</i>\n<code>+ Название предмета, макс_цена</code>"
            self.send_message(chat_id, msg_text)

        elif text.startswith("+") and "," in text:
            try:
                parts = text[1:].split(",")
                item_name = parts[0].strip()
                max_p = float(parts[1].strip())
                self.config["strategy"]["wishlist"].append({"name": item_name, "max_price": max_p})
                self._save_config()
                self.send_message(chat_id, f"✅ Добавлен в вишлист: <b>{item_name}</b> (до {max_p} CS)")
            except Exception:
                self.send_message(chat_id, "Формат: <code>+ Талик Невежества, 180</code>")

        elif text == "📊 Статистика бота":
            st = (
                f"📊 <b>Статистика Ultra-Fast HTTP/2 сессии:</b>\n"
                f"• Быстрых проверок (Fast-Check 0.1ms): <b>{self.stats['fast_checks']}</b>\n"
                f"• Полных парсингов при обновлении: <b>{self.stats['full_parses']}</b>\n"
                f"• Среднее время ответа HTTP/2: <b>{self.stats['avg_latency_ms']} мс</b>\n"
                f"• Срабатываний Tarpit защиты: <b>{self.stats['tarpit_detections']}</b>\n"
                f"• Найдено выгодных лотов: <b>{self.stats['deals_found']}</b>\n"
                f"• Куплено лотов: <b>{self.stats['purchased']}</b>\n"
                f"• Потрачено: <b>{self.stats['total_spent']} CS</b>\n"
                f"• Авто-выкуп: {'🟢 Активен' if self.config['strategy']['auto_buy'] else '🔴 Остановлен'}"
            )
            self.send_message(chat_id, st)

        elif text == "⚙️ Настройки":
            cfg = self.config["strategy"]
            st = (
                f"⚙️ <b>Параметры скорости и фильтров:</b>\n"
                f"• Пауза между проверками: <b>{cfg.get('poll_interval_sec')} сек</b>\n"
                f"• Мин. выгода: <b>от {cfg.get('min_profit_cs', 500)} CS</b>\n"
                f"• Вещи Тип C: <b>только $\\ge$ {cfg.get('min_type_c_level', 40)} ур.</b>\n"
                f"• Мин. скидка к конкурентам: <b>{cfg.get('min_discount_pct')}%</b>\n"
                f"• Макс. цена за 1 лот: <b>{cfg.get('max_price_per_lot')} CS</b>\n\n"
                f"<i>Команды для изменения настроек прямо в чате:</i>\n"
                f"• <code>интервал 0.4</code> — пауза в секундах\n"
                f"• <code>выгода 500</code> — мин. прибыль в CS\n"
                f"• <code>скидка 20</code> — мин. скидка в %\n"
                f"• <code>макс_цена 5000</code> — лимит цены лота"
            )
            self.send_message(chat_id, st)

        elif text.lower().startswith("интервал "):
            try:
                val = float(text.split()[1])
                self.config["strategy"]["poll_interval_sec"] = max(0.2, val)
                self._save_config()
                self.send_message(chat_id, f"✅ Пауза опроса установлена: <b>{val} сек</b>")
            except Exception:
                self.send_message(chat_id, "Пример: <code>интервал 0.4</code>")

        elif text.lower().startswith("выгода "):
            try:
                val = float(text.split()[1])
                self.config["strategy"]["min_profit_cs"] = val
                self._save_config()
                self.send_message(chat_id, f"✅ Порог выгоды установлен: <b>{val} CS</b>")
            except Exception:
                self.send_message(chat_id, "Пример: <code>выгода 500</code>")

        elif text.lower().startswith("скидка "):
            try:
                val = float(text.split()[1])
                self.config["strategy"]["min_discount_pct"] = val
                self._save_config()
                self.send_message(chat_id, f"✅ Минимальная скидка установлена: <b>{val}%</b>")
            except Exception:
                self.send_message(chat_id, "Пример: <code>скидка 25</code>")

        elif text.lower().startswith("макс_цена "):
            try:
                val = float(text.split()[1])
                self.config["strategy"]["max_price_per_lot"] = val
                self._save_config()
                self.send_message(chat_id, f"✅ Максимальная цена за лот: <b>{val} CS</b>")
            except Exception:
                self.send_message(chat_id, "Пример: <code>макс_цена 3000</code>")

    def handle_updates(self):
        logger.info("Обработчик команд Telegram активен.")
        while self.running:
            try:
                url = f"{self.api_url}/getUpdates?offset={self.last_update_id + 1}&timeout=15"
                resp = self.client.get(url, timeout=20.0)
                if resp.status_code != 200:
                    time.sleep(2)
                    continue

                data = resp.json()
                for update in data.get("result", []):
                    self.last_update_id = update["update_id"]
                    self.process_single_update(update)

            except Exception as e:
                logger.error(f"Ошибка TG Polling: {e}")
                time.sleep(2)

    def run(self):
        if self.token.startswith("YOUR_BOT_TOKEN") or not self.token:
            print("================================================================")
            print("ОШИБКА: Укажите токен вашего бота в tg_bot_config.json!")
            print("Получить токен бесплатно можно у официального бота @BotFather")
            print("================================================================")
            return

        # Запуск фонового HTTP-сервера для Bothost
        http_thread = threading.Thread(target=start_http_health_server, daemon=True)
        http_thread.start()

        # Запуск фонового высокоскоростного сканера
        scanner_thread = threading.Thread(target=self.background_speed_scanner, daemon=True)
        scanner_thread.start()

        # Запуск обработчика Telegram команд
        self.handle_updates()


if __name__ == "__main__":
    bot = FastTelegramMarketBot()
    bot.run()
