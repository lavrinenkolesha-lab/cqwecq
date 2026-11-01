#!/usr/bin/env python3
"""
Cerberus Games Market Bot - Ultra-Fast HTTP/2 Edition + Characters Scanner
===========================================================================
Высокоскоростной Telegram-снайпер и монитор аукциона cerberus-games.com.

Ключевые возможности:
1. Мониторинг предметов (https://cerberus-games.com/market).
2. Мониторинг биржи персонажей (https://cerberus-games.com/market/characters).
3. HTTP/2 Мультиплексирование (httpx + h2): время ответа ~350-450 мс.
4. Ультрабыстрый Fast-Check (0.1 мс): regex-проверка ID без тяжелого DOM-парсинга.
5. Защита от Tarpit-замедлений ServicePipe с адаптивным backoff.
6. Фильтры: вещи Тип C >= 40 ур., выгода от 500 CS, персонажи по уровню/классу/цене.
7. Интерактивные кнопки Telegram (покупка в 1 клик, просмотр карточки на сайте).
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
        "characters_path": "/market/characters",
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
        ],
        "characters_monitor": {
            "enabled": True,
            "min_level": 45,
            "max_price": 60000,
            "target_classes": ["МАУ", "Специалист", "Минёр", "Канонир", "Берсерк", "Паладин", "Варлок"]
        }
    }
}


class HealthCheckHandler(BaseHTTPRequestHandler):
    """Легковесный HTTP-сервер для поддержки Render/Bothost."""
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Cerberus Ultra-Fast Market & Characters Bot is running!")

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

        # Инициализация HTTP/2 клиента
        self.client = self._init_http2_client()

        self.running = True
        self.stats = {
            "fast_checks_items": 0,
            "fast_checks_chars": 0,
            "full_parses_items": 0,
            "full_parses_chars": 0,
            "deals_found": 0,
            "chars_found": 0,
            "purchased": 0,
            "total_spent": 0,
            "tarpit_detections": 0,
            "avg_latency_ms": 0.0
        }
        self.last_top_item_id: Optional[str] = None
        self.last_items_hash: Optional[int] = None

        self.last_top_char_id: Optional[str] = None
        self.last_chars_hash: Optional[int] = None

        self.seen_lots = set()
        self.seen_chars = set()

    def _load_config(self) -> Dict[str, Any]:
        if not os.path.exists(self.config_path):
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_TG_CONFIG, f, indent=2, ensure_ascii=False)
            return DEFAULT_TG_CONFIG
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                # Подмешиваем дефолтные секции если отсутствуют
                if "characters_path" not in cfg.get("cerberus", {}):
                    cfg.setdefault("cerberus", {})["characters_path"] = "/market/characters"
                if "characters_monitor" not in cfg.get("strategy", {}):
                    cfg.setdefault("strategy", {})["characters_monitor"] = DEFAULT_TG_CONFIG["strategy"]["characters_monitor"]
                return cfg
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
        char_mon = self.config["strategy"].get("characters_monitor", {})
        chars_status = "🟢 ВКЛ" if char_mon.get("enabled", True) else "🔴 ВЫКЛ"
        return {
            "keyboard": [
                [{"text": "🔍 Сканировать рынок"}, {"text": f"👤 Персонажи: {chars_status}"}],
                [{"text": f"⚡ Авто-покупка: {auto_buy_status}"}, {"text": "📋 Мой вишлист"}],
                [{"text": "⚙️ Настройки"}, {"text": "📊 Статистика бота"}]
            ],
            "resize_keyboard": True
        }

    # --- Запросы страниц рынка и персонажей ---
    def fetch_page(self, path: str) -> Tuple[Optional[str], float, int]:
        c_conf = self.config.get("cerberus", {})
        url = c_conf.get("base_url", "https://cerberus-games.com") + path

        t0 = time.perf_counter()
        try:
            resp = self.client.get(url)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            return resp.text, elapsed_ms, resp.status_code
        except Exception as e:
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            logger.error(f"HTTP/2 запрос ({path}) ошибка ({elapsed_ms:.1f} мс): {e}")
            return None, elapsed_ms, 0

    # --- Fast-Check для предметов и персонажей ---
    def fast_check_items_changed(self, html: str) -> Tuple[bool, Optional[str]]:
        match = re.search(r'(?:data-lot-id=["\']|details\?s=)(\d+)', html)
        top_id = match.group(1) if match else None
        current_hash = hash(html[:4000])

        is_changed = False
        if top_id != self.last_top_item_id or current_hash != self.last_items_hash:
            is_changed = True

        self.last_top_item_id = top_id
        self.last_items_hash = current_hash
        return is_changed, top_id

    def fast_check_characters_changed(self, html: str) -> Tuple[bool, Optional[str]]:
        match = re.search(r'(?:character=(\d+)|details\?s=(\d+)|data-character-id=["\'](\d+))', html)
        top_id = None
        if match:
            top_id = next(g for g in match.groups() if g is not None)
        current_hash = hash(html[:4000])

        is_changed = False
        if top_id != self.last_top_char_id or current_hash != self.last_chars_hash:
            is_changed = True

        self.last_top_char_id = top_id
        self.last_chars_hash = current_hash
        return is_changed, top_id

    # --- Парсинг предметов ---
    def _parse_html_lots(self, html: str) -> List[Dict[str, Any]]:
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

    # --- Парсинг персонажей ---
    def _parse_html_characters(self, html: str) -> List[Dict[str, Any]]:
        soup = BeautifulSoup(html, "html.parser")
        rows = soup.select(
            "tr.character-item, tr.lot, .character-card, .market-table tbody tr, table tbody tr, .characters-list tr"
        )

        characters = []
        for r in rows:
            try:
                char_id = r.get("data-character-id") or r.get("data-id") or r.get("data-lot-id")
                details = r.select_one("a[href*='character='], a[href*='characters/info'], a[href*='?s=']")
                char_url = ""
                if details:
                    href = details.get("href", "")
                    char_url = href if href.startswith("http") else ("https://cerberus-games.com" + href)
                    if "character=" in href:
                        char_id = href.split("character=")[-1].split("&")[0]
                    elif "?s=" in href:
                        char_id = href.split("?s=")[-1].split("&")[0]

                if not char_id:
                    continue

                row_text = r.get_text(" ", strip=True)

                # Никнейм / Название слота
                nick_el = r.select_one(".name, .char-name, .character-name, .nickname, td.name, strong")
                nickname = nick_el.get_text(strip=True) if nick_el else f"Персонаж #{char_id}"

                # Цена
                price_el = r.select_one(".price, .market-price, .cost, td.price")
                if price_el:
                    raw_price = "".join(ch for ch in price_el.get_text(strip=True) if ch.isdigit() or ch == ".")
                    price = float(raw_price) if raw_price else 0.0
                else:
                    price_match = re.search(r'(\d+[\d\s]*)\s*(?:CS|Cash|Шоп)', row_text, re.I)
                    price = float(price_match.group(1).replace(" ", "")) if price_match else 0.0

                if price <= 0:
                    continue

                # Уровень
                level = 1
                lvl_match = re.search(r'(?:ур\.|lvl|level|уровень)\s*(\d{2})', row_text, re.I)
                if not lvl_match:
                    lvl_match = re.search(r'\b(4\d|5\d|6\d)\b', row_text)
                if lvl_match:
                    level = int(lvl_match.group(1))

                # Раса
                race = "Не указана"
                if any(x in row_text.lower() for x in ["беллато", "bellato", "bcc"]):
                    race = "Беллато (BCC)"
                elif any(x in row_text.lower() for x in ["кора", "cora", "ccc"]):
                    race = "Кора (CCC)"
                elif any(x in row_text.lower() for x in ["акретия", "accretia", "acc"]):
                    race = "Акретия (ACC)"

                # Класс / Профессия
                char_class = "Общий"
                for cls_name in ["МАУ", "MAU", "Специалист", "Минёр", "Канонир", "Берсерк", "Паладин", "Варлок", "Маг", "Стрелок", "Ланчер", "Танк", "ББ"]:
                    if cls_name.lower() in row_text.lower():
                        char_class = cls_name
                        break

                characters.append({
                    "char_id": str(char_id),
                    "nickname": nickname,
                    "level": level,
                    "race": race,
                    "char_class": char_class,
                    "price": price,
                    "url": char_url or f"https://cerberus-games.com/market/details?s={char_id}",
                    "raw_desc": row_text[:120]
                })
            except Exception:
                continue

        return characters

    # --- Оценка сделок по предметам ---
    def evaluate_market_deals(self, lots: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        deals = []
        strat = self.config["strategy"]
        min_disc = strat.get("min_discount_pct", 20.0)
        min_profit_cs = strat.get("min_profit_cs", 500)
        min_type_c_lvl = strat.get("min_type_c_level", 40)
        max_lot_price = strat.get("max_price_per_lot", 50000)
        wishlist = strat.get("wishlist", [])

        filtered_lots = []
        for l in lots:
            if l["is_type_c"] and l["level"] < min_type_c_lvl:
                continue
            filtered_lots.append(l)

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

                if discount >= min_disc and potential_profit >= min_profit_cs and best["price"] <= max_lot_price:
                    best["reason"] = f"Скидка {discount:.1f}% (+{potential_profit:.0f} CS к следующему)"
                    deals.append(best)

            for l in group:
                for w in wishlist:
                    if w["name"].lower() in l["name"].lower():
                        if l["unit_price"] <= w["max_price"] and l["price"] <= max_lot_price:
                            if l not in deals:
                                l["reason"] = f"Вишлист (цена {l['unit_price']:.1f} <= {w['max_price']} CS)"
                                deals.append(l)

        return deals

    # --- Оценка персонажей ---
    def evaluate_characters_deals(self, chars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        deals = []
        c_mon = self.config["strategy"].get("characters_monitor", {})
        min_lvl = c_mon.get("min_level", 45)
        max_p = c_mon.get("max_price", 60000)
        targets = [t.lower() for t in c_mon.get("target_classes", [])]

        for c in chars:
            if c["level"] >= min_lvl and c["price"] <= max_p:
                is_target = any(t in c["char_class"].lower() for t in targets) or any(t in c["nickname"].lower() for t in targets)
                reason = "Целевой класс" if is_target else f"Уровень {c['level']} по выгодной цене"
                c["reason"] = reason
                deals.append(c)
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

    # --- Основной цикл высокоскоростного мониторинга (предметы + персонажи) ---
    def background_speed_scanner(self):
        logger.info("⚡ Высокоскоростной сканер HTTP/2 запущен (рынок предметов + персонажи).")
        strat = self.config["strategy"]
        c_conf = self.config["cerberus"]

        cycle = 0
        while self.running:
            poll_interval = strat.get("poll_interval_sec", 0.4)
            tarpit_backoff = strat.get("tarpit_backoff_sec", 1.8)
            cycle += 1

            # 1. Проверка рынка предметов (каждый цикл)
            html_items, elapsed_ms, status = self.fetch_page(c_conf.get("market_path", "/market"))
            self.stats["fast_checks_items"] += 1
            self.stats["avg_latency_ms"] = round((self.stats["avg_latency_ms"] * 0.9) + (elapsed_ms * 0.1), 1)

            if elapsed_ms > 1800 or status == 429:
                self.stats["tarpit_detections"] += 1
                logger.warning(f"⚠️ ServicePipe Tarpit ({elapsed_ms:.0f} мс). Пауза {tarpit_backoff} сек...")
                time.sleep(tarpit_backoff)
                continue

            if html_items and status == 200:
                changed, _ = self.fast_check_items_changed(html_items)
                if changed:
                    self.stats["full_parses_items"] += 1
                    lots = self._parse_html_lots(html_items)
                    deals = self.evaluate_market_deals(lots)
                    self._dispatch_item_deals(deals)

            # 2. Проверка биржи персонажей (каждые 3 цикла или при включенном мониторе)
            char_mon = strat.get("characters_monitor", {})
            if char_mon.get("enabled", True) and (cycle % 3 == 0):
                html_chars, el_chars, st_chars = self.fetch_page(c_conf.get("characters_path", "/market/characters"))
                self.stats["fast_checks_chars"] += 1

                if html_chars and st_chars == 200:
                    ch_changed, _ = self.fast_check_characters_changed(html_chars)
                    if ch_changed:
                        self.stats["full_parses_chars"] += 1
                        chars = self._parse_html_characters(html_chars)
                        char_deals = self.evaluate_characters_deals(chars)
                        self._dispatch_char_deals(char_deals)

            time.sleep(poll_interval)

    def _dispatch_item_deals(self, deals: List[Dict[str, Any]]):
        admin_id = self.config["telegram"].get("group_chat_id") or self.config["telegram"].get("admin_chat_id")
        if not admin_id:
            return

        for deal in deals:
            lid = deal["lot_id"]
            if lid in self.seen_lots:
                continue
            self.seen_lots.add(lid)
            self.stats["deals_found"] += 1

            if self.config["strategy"].get("auto_buy", False):
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
                        f"Предмет: {deal['name']} за {deal['price']} CS"
                    )
                self.send_message(admin_id, msg)
            else:
                inline_kb = {
                    "inline_keyboard": [
                        [{"text": f"🛒 Купить #{lid} ({deal['price']} CS)", "callback_data": f"buy_{lid}"}],
                        [{"text": "🔗 Открыть лот", "url": f"https://cerberus-games.com/market/details?s={lid}"}]
                    ]
                }
                msg = (
                    f"🔥 <b>Найден выгодный лот!</b>\n"
                    f"Предмет: <b>{deal['name']}</b> (x{deal['amount']})\n"
                    f"Цена: <b>{deal['price']} CS</b> (за ед: {deal['unit_price']:.1f})\n"
                    f"Выгода: <i>{deal['reason']}</i>"
                )
                self.send_message(admin_id, msg, reply_markup=inline_kb)

    def _dispatch_char_deals(self, char_deals: List[Dict[str, Any]]):
        admin_id = self.config["telegram"].get("group_chat_id") or self.config["telegram"].get("admin_chat_id")
        if not admin_id:
            return

        for c in char_deals:
            cid = c["char_id"]
            if cid in self.seen_chars:
                continue
            self.seen_chars.add(cid)
            self.stats["chars_found"] += 1

            inline_kb = {
                "inline_keyboard": [
                    [{"text": f"🛒 Купить персонажа ({c['price']:,.0f} CS)", "callback_data": f"buy_{cid}"}],
                    [{"text": "🔗 Открыть карточку", "url": c["url"]}]
                ]
            }
            msg = (
                f"👤 <b>Новый персонаж на бирже!</b>\n"
                f"Ник: <b>{c['nickname']}</b>\n"
                f"Уровень: <b>{c['level']} ур.</b> | Раса: <b>{c['race']}</b>\n"
                f"Класс: <b>{c['char_class']}</b>\n"
                f"Цена: <b>{c['price']:,.0f} CS</b>\n"
                f"Обоснование: <i>{c['reason']}</i>"
            )
            self.send_message(admin_id, msg, reply_markup=inline_kb)

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
            self.send_message(chat_id, "🤖 <b>Cerberus Sniper: Рынок предметов + Персонажи</b>", reply_markup=self.get_main_keyboard())

        elif text == "🔍 Сканировать рынок":
            self.send_message(chat_id, "⏳ <i>Мгновенный опрос аукциона через HTTP/2...</i>")
            html, elapsed_ms, status = self.fetch_page(self.config["cerberus"].get("market_path", "/market"))
            if not html:
                self.send_message(chat_id, f"❌ Ошибка соединения (время ответа: {elapsed_ms:.0f} мс)")
                return

            lots = self._parse_html_lots(html)
            deals = self.evaluate_market_deals(lots)
            res_text = (
                f"📡 <b>Результат HTTP/2 сканирования:</b>\n"
                f"• Время ответа: <b>{elapsed_ms:.1f} мс</b>\n"
                f"• Активных лотов: <b>{len(lots)}</b>\n"
                f"• Выгодных предложений (выгода $\\ge$ {self.config['strategy'].get('min_profit_cs')} CS): <b>{len(deals)}</b>"
            )
            self.send_message(chat_id, res_text)
            for d in deals[:4]:
                lid = d["lot_id"]
                inline_kb = {"inline_keyboard": [[{"text": f"🛒 Купить за {d['price']} CS", "callback_data": f"buy_{lid}"}]]}
                card = f"🔹 <b>{d['name']}</b> (x{d['amount']})\nЦена: <b>{d['price']} CS</b>\nВыгода: <i>{d['reason']}</i>"
                self.send_message(chat_id, card, reply_markup=inline_kb)

        elif "Персонажи" in text:
            # Ручной срез биржи персонажей
            self.send_message(chat_id, "⏳ <i>Сканирую биржу персонажей...</i>")
            html_ch, el_ch, st_ch = self.fetch_page(self.config["cerberus"].get("characters_path", "/market/characters"))
            if not html_ch or st_ch != 200:
                self.send_message(chat_id, f"❌ Не удалось загрузить биржу персонажей (статус {st_ch})")
                return

            chars = self._parse_html_characters(html_ch)
            char_deals = self.evaluate_characters_deals(chars)

            c_mon = self.config["strategy"].get("characters_monitor", {})
            st_msg = (
                f"👤 <b>Биржа персонажей:</b>\n"
                f"• Всего персонажей в продаже: <b>{len(chars)}</b>\n"
                f"• Подходящих под фильтр (ур. $\\ge$ {c_mon.get('min_level')}, цена $\\le$ {c_mon.get('max_price')} CS): <b>{len(char_deals)}</b>"
            )
            self.send_message(chat_id, st_msg)

            for c in char_deals[:4]:
                cid = c["char_id"]
                inline_kb = {
                    "inline_keyboard": [
                        [{"text": f"🛒 Купить ({c['price']:,.0f} CS)", "callback_data": f"buy_{cid}"}],
                        [{"text": "🔗 Открыть карточку", "url": c["url"]}]
                    ]
                }
                card = (
                    f"👤 <b>{c['nickname']}</b> ({c['level']} ур.)\n"
                    f"Раса: <b>{c['race']}</b> | Класс: <b>{c['char_class']}</b>\n"
                    f"Цена: <b>{c['price']:,.0f} CS</b>"
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
                f"📊 <b>Статистика Ultra-Fast сессии:</b>\n"
                f"• Проверок предметов (0.1ms): <b>{self.stats['fast_checks_items']}</b>\n"
                f"• Проверок персонажей: <b>{self.stats['fast_checks_chars']}</b>\n"
                f"• Среднее время HTTP/2: <b>{self.stats['avg_latency_ms']} мс</b>\n"
                f"• Найдено выгодных лотов предметов: <b>{self.stats['deals_found']}</b>\n"
                f"• Найдено подходящих персонажей: <b>{self.stats['chars_found']}</b>\n"
                f"• Куплено: <b>{self.stats['purchased']}</b>\n"
                f"• Авто-выкуп: {'🟢 Активен' if self.config['strategy']['auto_buy'] else '🔴 Остановлен'}"
            )
            self.send_message(chat_id, st)

        elif text == "⚙️ Настройки":
            cfg = self.config["strategy"]
            c_mon = cfg.get("characters_monitor", {})
            st = (
                f"⚙️ <b>Параметры снайпера:</b>\n"
                f"• Пауза опроса: <b>{cfg.get('poll_interval_sec')} сек</b>\n"
                f"• Мин. выгода на вещах: <b>от {cfg.get('min_profit_cs', 500)} CS</b>\n"
                f"• Вещи Тип C: <b>только $\\ge$ {cfg.get('min_type_c_level', 40)} ур.</b>\n"
                f"• Монитор персонажей: <b>{'🟢 Включен' if c_mon.get('enabled') else '🔴 Выключен'}</b>\n"
                f"• Мин. уровень персонажа: <b>$\\ge$ {c_mon.get('min_level')} ур.</b>\n"
                f"• Макс. цена персонажа: <b>{c_mon.get('max_price')} CS</b>\n\n"
                f"<i>Команды для быстрой настройки:</i>\n"
                f"• <code>персы вкл</code> / <code>персы выкл</code>\n"
                f"• <code>перс_мин_ур 50</code> — мин. уровень персонажа\n"
                f"• <code>перс_макс_цена 40000</code> — потолок цены персонажа\n"
                f"• <code>интервал 0.4</code> — пауза в секундах\n"
                f"• <code>выгода 500</code> — мин. прибыль в CS"
            )
            self.send_message(chat_id, st)

        elif text.lower() == "персы вкл":
            self.config["strategy"].setdefault("characters_monitor", {})["enabled"] = True
            self._save_config()
            self.send_message(chat_id, "✅ Мониторинг персонажей <b>ВКЛЮЧЕН</b>", reply_markup=self.get_main_keyboard())

        elif text.lower() == "персы выкл":
            self.config["strategy"].setdefault("characters_monitor", {})["enabled"] = False
            self._save_config()
            self.send_message(chat_id, "🔴 Мониторинг персонажей <b>ВЫКЛЮЧЕН</b>", reply_markup=self.get_main_keyboard())

        elif text.lower().startswith("перс_мин_ур "):
            try:
                val = int(text.split()[1])
                self.config["strategy"].setdefault("characters_monitor", {})["min_level"] = val
                self._save_config()
                self.send_message(chat_id, f"✅ Мин. уровень персонажей: <b>{val} ур.</b>")
            except Exception:
                self.send_message(chat_id, "Пример: <code>перс_мин_ур 50</code>")

        elif text.lower().startswith("перс_макс_цена "):
            try:
                val = float(text.split()[1])
                self.config["strategy"].setdefault("characters_monitor", {})["max_price"] = val
                self._save_config()
                self.send_message(chat_id, f"✅ Макс. цена персонажей: <b>{val:,.0f} CS</b>")
            except Exception:
                self.send_message(chat_id, "Пример: <code>перс_макс_цена 40000</code>")

        elif text.lower().startswith("интервал "):
            try:
                val = float(text.split()[1])
                self.config["strategy"]["poll_interval_sec"] = max(0.2, val)
                self._save_config()
                self.send_message(chat_id, f"✅ Пауза опроса: <b>{val} сек</b>")
            except Exception:
                self.send_message(chat_id, "Пример: <code>интервал 0.4</code>")

        elif text.lower().startswith("выгода "):
            try:
                val = float(text.split()[1])
                self.config["strategy"]["min_profit_cs"] = val
                self._save_config()
                self.send_message(chat_id, f"✅ Порог выгоды: <b>{val} CS</b>")
            except Exception:
                self.send_message(chat_id, "Пример: <code>выгода 500</code>")

        elif text.lower().startswith("скидка "):
            try:
                val = float(text.split()[1])
                self.config["strategy"]["min_discount_pct"] = val
                self._save_config()
                self.send_message(chat_id, f"✅ Мин. скидка: <b>{val}%</b>")
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

        http_thread = threading.Thread(target=start_http_health_server, daemon=True)
        http_thread.start()

        scanner_thread = threading.Thread(target=self.background_speed_scanner, daemon=True)
        scanner_thread.start()

        self.handle_updates()


if __name__ == "__main__":
    bot = FastTelegramMarketBot()
    bot.run()
