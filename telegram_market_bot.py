#!/usr/bin/env python3
"""
Cerberus Games Market - Telegram Bot Controller
================================================
Control your Cerberus auction sniper entirely from Telegram.
No local installation required if deployed on free cloud hosts (Render, Replit, Railway, PythonAnywhere).

Features:
- Interactive Telegram keyboard (Scan Now, Toggle Auto-Buy, Wishlist, Settings, Stats).
- Real-time deal alert cards with inline [⚡ Купить в 1 клик] buttons.
- Continuous background scanner polling cerberus-games.com/market.
- Uses lightweight standard 'requests' long-polling (no complex dependencies).
"""

import os
import sys
import time
import json
import random
import logging
import threading
from typing import Dict, Any, List, Optional

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Run: pip install requests beautifulsoup4")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("CerberusTgBot")

BOT_CONFIG_FILE = "tg_bot_config.json"

DEFAULT_TG_CONFIG = {
    "telegram": {
        "bot_token": "YOUR_BOT_TOKEN_FROM_BOTFATHER",
        "admin_chat_id": 0  # Automatically locked to the first user who sends /start
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
            "Referer": "https://cerberus-games.com/market"
        }
    },
    "strategy": {
        "auto_buy": False,
        "min_discount_pct": 20.0,
        "max_price_per_lot": 5000,
        "poll_interval_sec": 12,
        "wishlist": [
            {"name": "Талик Невежества", "max_price": 180},
            {"name": "Талик Покровительства", "max_price": 140},
            {"name": "Руда Т5", "max_price": 15}
        ]
    }
}


class TelegramBotManager:
    def __init__(self, config_path: str = BOT_CONFIG_FILE):
        self.config_path = config_path
        self.config = self._load_config()
        self.token = self.config["telegram"]["bot_token"]
        self.api_url = f"https://api.telegram.org/bot{self.token}"
        self.last_update_id = 0
        self.session = requests.Session()
        self._init_session()

        self.running = True
        self.stats = {"scanned_cycles": 0, "deals_found": 0, "purchased": 0, "total_spent": 0}
        self.seen_lots = set()

    def _load_config(self) -> Dict[str, Any]:
        if not os.path.exists(self.config_path):
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_TG_CONFIG, f, indent=2, ensure_ascii=False)
            return DEFAULT_TG_CONFIG
        with open(self.config_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save_config(self):
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(self.config, f, indent=2, ensure_ascii=False)

    def _init_session(self):
        c_conf = self.config.get("cerberus", {})
        self.session.headers.update(c_conf.get("headers", {}))
        for k, v in c_conf.get("cookies", {}).items():
            if v:
                self.session.cookies.set(k, v, domain="cerberus-games.com")

    # --- Telegram API Helpers ---
    def send_message(self, chat_id: int, text: str, reply_markup: Optional[Dict] = None) -> bool:
        if not chat_id:
            return False
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            r = requests.post(f"{self.api_url}/sendMessage", json=payload, timeout=10)
            return r.status_code == 200
        except Exception as e:
            logger.error(f"Error sending TG message: {e}")
            return False

    def answer_callback(self, callback_query_id: str, text: str = ""):
        try:
            requests.post(f"{self.api_url}/answerCallbackQuery", json={
                "callback_query_id": callback_query_id,
                "text": text
            }, timeout=5)
        except Exception as e:
            logger.debug(f"Error answering callback: {e}")

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

    # --- Market Parsing & Logic ---
    def fetch_lots(self) -> List[Dict[str, Any]]:
        c_conf = self.config.get("cerberus", {})
        url = c_conf.get("base_url", "https://cerberus-games.com") + c_conf.get("market_path", "/market")
        try:
            resp = self.session.get(url, timeout=15)
            if resp.status_code != 200:
                logger.warning(f"Market fetch status: {resp.status_code}")
                return []
            return self._parse_html(resp.text)
        except Exception as e:
            logger.error(f"Fetch market error: {e}")
            return []

    def _parse_html(self, html: str) -> List[Dict[str, Any]]:
        soup = BeautifulSoup(html, "html.parser")
        rows = soup.select("tr.market-item, tr.lot, .market-table tbody tr, .market-grid .market-item")
        if not rows:
            rows = soup.select("table tbody tr")

        lots = []
        for r in rows:
            try:
                # Lot ID
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

                if price > 0:
                    lots.append({
                        "lot_id": str(lot_id),
                        "name": name,
                        "amount": amount,
                        "price": price,
                        "unit_price": price / amount
                    })
            except Exception as e:
                continue
        return lots

    def evaluate_market(self, lots: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        deals = []
        strat = self.config["strategy"]
        min_disc = strat.get("min_discount_pct", 20.0)
        max_lot_price = strat.get("max_price_per_lot", 5000)
        wishlist = strat.get("wishlist", [])

        # Group by name
        groups = {}
        for l in lots:
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
                if discount >= min_disc and best["price"] <= max_lot_price:
                    best["reason"] = f"Скидка {discount:.1f}% (следующий: {second['unit_price']:.1f} CS)"
                    deals.append(best)

            for l in group:
                for w in wishlist:
                    if w["name"].lower() in l["name"].lower():
                        if l["unit_price"] <= w["max_price"] and l["price"] <= max_lot_price:
                            if l not in deals:
                                l["reason"] = f"Вишлист: {l['unit_price']:.1f} <= {w['max_price']} CS"
                                deals.append(l)

        return deals

    def execute_buy(self, lot_id: str) -> bool:
        c_conf = self.config.get("cerberus", {})
        url = c_conf.get("base_url", "https://cerberus-games.com") + c_conf.get("buy_endpoint", "/market/buy")
        payload = {"lot_id": lot_id, "confirm": 1}
        try:
            r = self.session.post(url, data=payload, timeout=10)
            return r.status_code == 200 and ("success" in r.text.lower() or "ok" in r.text.lower())
        except Exception as e:
            logger.error(f"Buy error: {e}")
            return False

    # --- Background Polling Worker ---
    def background_scanner(self):
        logger.info("Background market scanner started.")
        while self.running:
            try:
                admin_id = self.config["telegram"].get("admin_chat_id")
                if admin_id:
                    lots = self.fetch_lots()
                    self.stats["scanned_cycles"] += 1
                    deals = self.evaluate_market(lots)

                    for deal in deals:
                        lid = deal["lot_id"]
                        if lid in self.seen_lots:
                            continue
                        self.seen_lots.add(lid)
                        self.stats["deals_found"] += 1

                        # If auto-buy is enabled
                        if self.config["strategy"]["auto_buy"]:
                            success = self.execute_buy(lid)
                            if success:
                                self.stats["purchased"] += 1
                                self.stats["total_spent"] += deal["price"]
                                msg = (
                                    f"✅ <b>[АВТО-ВЫКУП] Успешная сделка!</b>\n"
                                    f"Предмет: <b>{deal['name']}</b> (x{deal['amount']})\n"
                                    f"Цена: <b>{deal['price']} CS</b> (за шт: {deal['unit_price']:.1f})\n"
                                    f"Причина: {deal['reason']}\n"
                                    f"Ссылка: https://cerberus-games.com/market/details?s={lid}"
                                )
                                self.send_message(admin_id, msg)
                            else:
                                msg = (
                                    f"⚠️ <b>[ОШИБКА ПОКУПКИ] Лот #{lid}</b>\n"
                                    f"Предмет: {deal['name']} за {deal['price']} CS\n"
                                    f"Возможно, лот уже перекупили или истекла кука."
                                )
                                self.send_message(admin_id, msg)
                        else:
                            # Send Alert with Buy Button
                            inline_kb = {
                                "inline_keyboard": [
                                    [{"text": f"🛒 Купить #{lid} ({deal['price']} CS)", "callback_data": f"buy_{lid}"}],
                                    [{"text": "🔗 Открыть на сайте", "url": f"https://cerberus-games.com/market/details?s={lid}"}]
                                ]
                            }
                            msg = (
                                f"🔥 <b>Найден выгодный лот!</b>\n"
                                f"Предмет: <b>{deal['name']}</b> (x{deal['amount']})\n"
                                f"Цена: <b>{deal['price']} CS</b> (за шт: {deal['unit_price']:.1f})\n"
                                f"Выгода: <i>{deal['reason']}</i>"
                            )
                            self.send_message(admin_id, msg, reply_markup=inline_kb)

            except Exception as e:
                logger.error(f"Scanner exception: {e}")

            interval = self.config["strategy"].get("poll_interval_sec", 12)
            time.sleep(interval + random.uniform(1, 4))

    # --- Telegram Updates Polling ---
    def handle_updates(self):
        logger.info("Telegram command polling started.")
        while self.running:
            try:
                url = f"{self.api_url}/getUpdates?offset={self.last_update_id + 1}&timeout=20"
                resp = requests.get(url, timeout=25)
                if resp.status_code != 200:
                    time.sleep(3)
                    continue

                data = resp.json()
                for update in data.get("result", []):
                    self.last_update_id = update["update_id"]
                    self.process_single_update(update)

            except Exception as e:
                logger.error(f"TG Polling error: {e}")
                time.sleep(3)

    def process_single_update(self, update: Dict[str, Any]):
        # Callback query (Inline Button clicks)
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

        # Regular Message
        msg = update.get("message")
        if not msg:
            return

        chat_id = msg["chat"]["id"]
        text = msg.get("text", "").strip()

        # Authorize admin on /start
        current_admin = self.config["telegram"].get("admin_chat_id")
        if not current_admin:
            self.config["telegram"]["admin_chat_id"] = chat_id
            self._save_config()
            self.send_message(chat_id, "👑 <b>Вы успешно авторизованы как администратор бота!</b>", reply_markup=self.get_main_keyboard())
            return

        if chat_id != current_admin:
            self.send_message(chat_id, "⛔ Доступ ограничен. Этот бот привязан к другому администратору.")
            return

        # Commands
        if text in ("/start", "/menu"):
            self.send_message(chat_id, "🤖 <b>Панель управления Cerberus Market Sniper</b>", reply_markup=self.get_main_keyboard())

        elif text == "🔍 Сканировать рынок":
            self.send_message(chat_id, "⏳ <i>Сканирую активные лоты на Cerberus...</i>")
            lots = self.fetch_lots()
            deals = self.evaluate_market(lots)
            if not deals:
                self.send_message(chat_id, f"Найдено лотов: {len(lots)}. Недооцененных предложений прямо сейчас нет.")
            else:
                self.send_message(chat_id, f"🎯 Найдено выгодных предложений: <b>{len(deals)}</b>")
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
            except Exception as e:
                self.send_message(chat_id, "Формат: <code>+ Талик Невежества, 180</code>")

        elif text == "📊 Статистика бота":
            st = (
                f"📊 <b>Статистика текущей сессии:</b>\n"
                f"• Просканировано циклов: <b>{self.stats['scanned_cycles']}</b>\n"
                f"• Найдено выгодных лотов: <b>{self.stats['deals_found']}</b>\n"
                f"• Куплено лотов: <b>{self.stats['purchased']}</b>\n"
                f"• Потрачено: <b>{self.stats['total_spent']} CS</b>\n"
                f"• Режим авто-выкупа: {'🟢 Активен' if self.config['strategy']['auto_buy'] else '🔴 Остановлен'}"
            )
            self.send_message(chat_id, st)

        elif text == "⚙️ Настройки":
            cfg = self.config["strategy"]
            st = (
                f"⚙️ <b>Текущие настройки:</b>\n"
                f"• Мин. скидка к конкурентам: <b>{cfg.get('min_discount_pct')}%</b>\n"
                f"• Макс. цена на 1 лот: <b>{cfg.get('max_price_per_lot')} CS</b>\n"
                f"• Интервал сканирования: <b>{cfg.get('poll_interval_sec')} сек</b>\n\n"
                f"<i>Чтобы изменить мин. скидку, напишите:</i> <code>скидка 25</code>\n"
                f"<i>Чтобы изменить макс. цену, напишите:</i> <code>макс_цена 3000</code>"
            )
            self.send_message(chat_id, st)

        elif text.lower().startswith("скидка "):
            val = float(text.split()[1])
            self.config["strategy"]["min_discount_pct"] = val
            self._save_config()
            self.send_message(chat_id, f"✅ Минимальная скидка установлена на: <b>{val}%</b>")

        elif text.lower().startswith("макс_цена "):
            val = float(text.split()[1])
            self.config["strategy"]["max_price_per_lot"] = val
            self._save_config()
            self.send_message(chat_id, f"✅ Максимальная цена за лот: <b>{val} CS</b>")

    def run(self):
        if self.token.startswith("YOUR_BOT_TOKEN"):
            print("================================================================")
            print("ОШИБКА: Укажите токен вашего бота в tg_bot_config.json!")
            print("Получить токен бесплатно можно у официального бота @BotFather")
            print("================================================================")
            return

        t = threading.Thread(target=self.background_scanner, daemon=True)
        t.start()
        self.handle_updates()


if __name__ == "__main__":
    bot = TelegramBotManager()
    bot.run()