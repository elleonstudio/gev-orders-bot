import os
import logging
import math
import json
import re
import traceback
from datetime import datetime
from notion_client import Client
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ConversationHandler, filters, ContextTypes

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
NOTION_TOKEN = os.getenv('NOTION_TOKEN')
NOTION_DATABASE_ID = os.getenv('NOTION_DATABASE_ID')
PACKAGES_DATABASE_ID = "32a8c4d1fb0e806ebb98f5995704d0e5"

# Проверяем наличие токенов
if not TELEGRAM_TOKEN:
    logger.error("❌ TELEGRAM_BOT_TOKEN не найден!")
if not NOTION_TOKEN:
    logger.error("❌ NOTION_TOKEN не найден!")
if not NOTION_DATABASE_ID:
    logger.error("❌ NOTION_DATABASE_ID не найден!")

# Notion клиент (инициализируем если есть токены)
notion = None
if NOTION_TOKEN:
    try:
        notion = Client(auth=NOTION_TOKEN)
        logger.info("✅ Notion клиент создан")
    except Exception as e:
        logger.error(f"❌ Ошибка создания Notion клиента: {e}")

FF_BOX_PRICE = 2  # ¥ за коробку
orders = {}
TARIFFS = {
    'Коледино': 350,
    'Невинномысск': 1100,
    'Электросталь': 400,
    'Белые Столбы': 350,
    'Чашниково': 350,
    'Санкт-Петербург': 450,
    'Казань': 450,
    'Екатеринбург': 700,
    'Новосибирск': 850,
    'Владивосток': 1000,
    'Краснодар': 550,
}

def save_session():
    try:
        with open('orders_session.json', 'w', encoding='utf-8') as f:
            json.dump(orders, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Ошибка сохранения сессии: {e}")

def load_session():
    global orders
    try:
        with open('orders_session.json', 'r', encoding='utf-8') as f:
            orders = json.load(f)
    except:
        orders = {}

def fmt(n):
    return int(n) if n == int(n) else n

def get_code(client):
    today = datetime.now()
    return f"{client.upper().replace(' ', '-')}-{today.strftime('%y%m%d')}"

def calculate_boxes(l, w, h, qty):
    """Расчёт количества коробок с учётом макс. размеров 60×40×40"""
    MAX_L, MAX_W, MAX_H = 60, 40, 40
    
    items_per_box_l = max(1, int(MAX_L // l))
    items_per_box_w = max(1, int(MAX_W // w))
    items_per_box_h = max(1, int(MAX_H // h))
    items_per_box = items_per_box_l * items_per_box_w * items_per_box_h
    
    boxes = math.ceil(qty / items_per_box)
    return items_per_box, boxes

def optimize_boxes(items):
    """
    Оптимальная упаковка товаров в коробки.
    Смешивает разные товары для минимизации количества коробок.
    
    items: список {'name': str, 'qty': int, 'dims': (l, w, h), 'volume': float}
    
    Возвращает: список коробок [{'items': [...], 'total_volume': float, 'dims': (L, W, H)}]
    """
    MAX_L, MAX_W, MAX_H = 60, 40, 40
    BOX_VOLUME = MAX_L * MAX_W * MAX_H
    
    # Разбиваем все товары на единичные экземпляры с размерами
    all_items = []
    for item in items:
        l, w, h = item['dims']
        volume = l * w * h
        for i in range(item['qty']):
            all_items.append({
                'name': item['name'],
                'dims': (l, w, h),
                'volume': volume,
                'original_idx': len(all_items)
            })
    
    # Сортируем по объёму (от большего к меньшему) для лучшей упаковки
    all_items.sort(key=lambda x: x['volume'], reverse=True)
    
    # Алгоритм First Fit Decreasing
    boxes = []
    
    for item in all_items:
        l, w, h = item['dims']
        placed = False
        
        # Пробуем поместить в существующую коробку
        for box in boxes:
            # Проверяем, влезает ли по объёму
            if box['remaining_volume'] >= item['volume']:
                # Проверяем, можно ли физически разместить
                # Упрощённая проверка: по каждому измерению
                can_fit = False
                for rot_l, rot_w, rot_h in [
                    (l, w, h), (l, h, w), (w, l, h), 
                    (w, h, l), (h, l, w), (h, w, l)
                ]:
                    # Проверяем, есть ли место в коробке
                    remaining_l = MAX_L - box['used_l']
                    remaining_w = MAX_W - box['used_w']
                    remaining_h = MAX_H - box['used_h']
                    
                    if rot_l <= remaining_l and rot_w <= remaining_w and rot_h <= remaining_h:
                        can_fit = True
                        box['used_l'] += rot_l
                        box['used_w'] += rot_w
                        box['used_h'] += rot_h
                        break
                
                if can_fit:
                    box['items'].append(item)
                    box['remaining_volume'] -= item['volume']
                    placed = True
                    break
        
        # Если не поместилось — создаём новую коробку
        if not placed:
            boxes.append({
                'items': [item],
                'total_volume': BOX_VOLUME,
                'remaining_volume': BOX_VOLUME - item['volume'],
                'used_l': l,
                'used_w': w,
                'used_h': h,
                'dims': (MAX_L, MAX_W, MAX_H)
            })
    
    return boxes

def calculate_boxes_optimized(items):
    """
    Расчёт коробок с оптимизацией.
    items: список {'name': str, 'qty': int, 'dims': (l, w, h)}
    
    Возвращает: (total_boxes, boxes_details)
    """
    boxes = optimize_boxes(items)
    return len(boxes), boxes

async def get_packages_from_notion():
    """Получаем пакеты из базы Notion"""
    if not notion or not PACKAGES_DATABASE_ID:
        return []
    try:
        res = notion.databases.query(database_id=PACKAGES_DATABASE_ID)
        packages = []
        for page in res.get('results', []):
            props = page['properties']
            name = props.get('Название', {}).get('title', [{}])[0].get('text', {}).get('content', '')
            price = props.get('Цена', {}).get('number', 0)
            l = props.get('Длина', {}).get('number', 0)
            w = props.get('Ширина', {}).get('number', 0)
            h = props.get('Высота', {}).get('number', 0)
            if name and price and l and w and h:
                packages.append({'name': name, 'price': price, 'l': l, 'w': w, 'h': h, 'volume': l*w*h})
        return packages
    except Exception as e:
        logger.error(f"Ошибка получения пакетов: {e}")
        return []

def find_best_package(packages, l, w, h):
    """Находим минимальный подходящий пакет"""
    item_volume = l * w * h
    suitable = [p for p in packages if p['l'] >= l and p['w'] >= w and p['h'] >= h]
    if not suitable:
        return None
    return min(suitable, key=lambda p: p['volume'])

async def get_client_orders_from_notion(client_name):
    """Получаем все заказы клиента из Notion с обработкой ошибок (регистронезависимый поиск)"""
    if not notion or not NOTION_DATABASE_ID:
        return None, "Notion не настроен"
    
    try:
        # Получаем последние 100 записей (без фильтра для case-insensitive поиска)
        res = notion.databases.query(
            database_id=NOTION_DATABASE_ID,
            sorts=[{"timestamp": "created_time", "direction": "descending"}],
            page_size=100
        )
        
        # Фильтруем в Python с case-insensitive сравнением
        client_name_lower = client_name.lower()
        filtered_results = []
        for page in res.get('results', []):
            props = page['properties']
            # Получаем имя клиента из select поля
            client_select = props.get('Клиент', {})
            if client_select.get('select'):
                notion_client_name = client_select['select'].get('name', '')
                # Case-insensitive сравнение
                if notion_client_name.lower() == client_name_lower:
                    filtered_results.append(page)
        
        orders_list = []
        for page in filtered_results[:10]:  # Берём только первые 10 совпадений
            props = page['properties']
            created = page.get('created_time', '')[:10]
            # Безопасное получение описания (может быть пустым)
            rich_text = props.get('Описание товара', {}).get('rich_text', [])
            items_text = rich_text[0].get('text', {}).get('content', '') if rich_text else ''
            items_list = []
            seen_names = set()  # Для дедупликации
            for item_str in items_text.split(';'):
                item_str = item_str.strip()
                if item_str:
                    # Пробуем разные форматы: "Name × 100", "Name x 100", "Name 100"
                    name = item_str
                    qty = 0
                    
                    # Ищем × ( multiplication sign)
                    if '×' in item_str:
                        parts = item_str.rsplit('×', 1)
                        name = parts[0].strip()
                        try:
                            qty = int(parts[1].strip().split()[0])  # Берём только число
                        except:
                            qty = 0
                    # Ищем x (латинская)
                    elif ' x ' in item_str.lower():
                        parts = item_str.lower().rsplit(' x ', 1)
                        name = item_str[:item_str.lower().rfind(' x ')].strip()
                        try:
                            qty = int(parts[1].strip().split()[0])
                        except:
                            qty = 0
                    # Пробуем взять последнее число как количество
                    else:
                        numbers = re.findall(r'\d+', item_str)
                        if numbers:
                            try:
                                qty = int(numbers[-1])
                                # Убираем число из названия
                                name = re.sub(r'\s*\d+\s*$', '', item_str).strip()
                            except:
                                qty = 0
                    
                    # Пропускаем дубликаты по названию
                    if name and name not in seen_names:
                        seen_names.add(name)
                        items_list.append({'name': name, 'qty': qty})
            
            # Получаем размеры из Notion (поле "Размеры (Д×Ш×В)")
            dims_rich = props.get('Размеры (Д×Ш×В)', {}).get('rich_text', [])
            dimensions_str = dims_rich[0].get('text', {}).get('content', '') if dims_rich else ''
            
            # Парсим размеры для каждого товара
            parsed_dims = []
            if dimensions_str:
                # Формат: "16 12 5; 20 15 10" или "16×12×5; 20×15×10"
                for dim_part in dimensions_str.split(';'):
                    dim_part = dim_part.strip()
                    if dim_part:
                        # Ищем 3 числа
                        nums = re.findall(r'\d+\.?\d*', dim_part)
                        if len(nums) >= 3:
                            try:
                                parsed_dims.append((float(nums[0]), float(nums[1]), float(nums[2])))
                            except:
                                parsed_dims.append((0, 0, 0))
                        else:
                            parsed_dims.append((0, 0, 0))
            
            # Присваиваем размеры товарам
            for idx, item in enumerate(items_list):
                if idx < len(parsed_dims):
                    item['dims'] = parsed_dims[idx]
                    item['dimensions'] = f"{int(parsed_dims[idx][0])}×{int(parsed_dims[idx][1])}×{int(parsed_dims[idx][2])}"
                else:
                    item['dims'] = (0, 0, 0)
                    item['dimensions'] = ''
            
            # Безопасное получение кода заказа
            title_list = props.get('Код заказа', {}).get('title', [])
            code_text = title_list[0].get('text', {}).get('content', '') if title_list else ''
            
            order = {
                'id': page['id'],
                'code': code_text,
                'date': created,
                'client_rate': props.get('Курс клиенту', {}).get('number'),
                'real_rate': props.get('Курс реальный', {}).get('number'),
                'rub_rate': props.get('Курс ₽→драм', {}).get('number'),
                'items_text': items_text,
                'items': items_list,
                'total': props.get('К ОПЛАТЕ (AMD)', {}).get('number') or props.get('Прибыль (AMD)', {}).get('number'),
            }
            orders_list.append(order)
        return orders_list, None
    except Exception as e:
        error_msg = f"{type(e).__name__}: {str(e)}"
        logger.error(f"Error fetching client orders: {error_msg}")
        logger.error(traceback.format_exc())
        return None, error_msg

async def get_notion_fields():
    if not notion or not NOTION_DATABASE_ID:
        return ["Notion не настроен"]
    try:
        res = notion.databases.retrieve(database_id=NOTION_DATABASE_ID)
        fields = []
        for name, prop in res['properties'].items():
            fields.append(f"{name} ({prop['type']})")
        return fields
    except Exception as e:
        return [f"Ошибка: {e}"]

# ======== МЕНЮ ========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Проверяем доступ к Notion
    has_access, error = await check_notion_access()
    
    if has_access:
        notion_status = "✅ Notion подключен"
    else:
        notion_status = f"⚠️ Notion: {error[:50]}..." if len(error) > 50 else f"⚠️ Notion: {error}"
    
    menu = (
        f"🤖 <b>GS Orders Bot v41</b>\n"
        f"{notion_status}\n\n"
        "📋 <b>/zakaz [имя]</b> — Новый заказ\n"
        "📦 <b>/ff</b> — FF Китай\n"
        "🚚 <b>/dostavka</b> — FILLX РФ\n"
        "❌ <b>/cancel</b> — Отмена"
    )
    await update.message.reply_text(menu, parse_mode='HTML')

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('❌ Отменено. Начни сначала: /zakaz, /ff или /dostavka')
    return ConversationHandler.END

async def check_notion_access():
    """Проверяем доступ к Notion базе"""
    if not notion or not NOTION_DATABASE_ID:
        return False, "Notion не настроен (отсутствуют токены)"
    try:
        notion.databases.retrieve(database_id=NOTION_DATABASE_ID)
        return True, None
    except Exception as e:
        return False, str(e)

# ======== /ZAKAZ ========

Z_INVOICE, Z_SELECT_ORDER, Z_ORDER_ACTION, Z_SELECT_ITEMS, Z_EDIT_ITEM_QTY, Z_NAME, Z_QTY, Z_PRICE, Z_PURCHASE, Z_DELIVERY, Z_BUNDLE_SELECT, Z_BUNDLE_NEW, Z_BUNDLE_NEW_FROM_CURRENT, Z_DIMS, Z_MORE, Z_CLIENT_RATE, Z_REAL_RATE, Z_COMMISSION = range(18)

async def cmd_zakaz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    
    if context.args:
        client_name = ' '.join(context.args)
        
        # Получаем заказы клиента
        client_orders, error = await get_client_orders_from_notion(client_name)
        
        if client_orders is None:
            # Ошибка при получении — работаем без базы
            logger.error(f"Ошибка получения заказов: {error}")
            orders[uid] = {
                'client': client_name,
                'items': [],
                'type': 'zakaz',
                'all_client_orders': [],
                'notion_error': error
            }
            await update.message.reply_text(
                f'⚠️ <b>Notion временно недоступен</b>\n\n'
                f'Работаю в автономном режиме (расчёты работают, но не сохраняются).\n\n'
                f'Клиент: <b>{client_name}</b>\n'
                f'Название товара:',
                parse_mode='HTML'
            )
            return Z_NAME
        
        orders[uid] = {
            'client': client_name,
            'items': [],
            'type': 'zakaz',
            'all_client_orders': client_orders
        }
        
        if client_orders:
            # Показываем список заказов
            keyboard = []
            for idx, order in enumerate(client_orders[:5]):
                # Формируем описание товаров вместо даты
                items_list = order.get('items', [])
                if items_list:
                    # Показываем только названия товаров (количество в Notion не хранится отдельно)
                    items_names = [i['name'][:12] for i in items_list[:3] if i.get('name')]
                    items_desc = ", ".join(items_names)
                    if len(items_list) > 3:
                        items_desc += f" +{len(items_list)-3}"
                else:
                    items_desc = order.get('items_text', 'Товар')[:25]
                
                btn_text = f"📦 {items_desc}"
                keyboard.append([InlineKeyboardButton(btn_text, callback_data=f'z_sel_{idx}')])
            
            keyboard.append([InlineKeyboardButton("➕ Новый заказ", callback_data='z_sel_new')])
            
            await update.message.reply_text(
                f'Клиент: <b>{client_name}</b>\n'
                f'Найдено заказов: {len(client_orders)}\n\n'
                f'Выбери заказ или создай новый:',
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='HTML'
            )
            return Z_SELECT_ORDER
        else:
            # Новый клиент без заказов — спрашиваем про инвойс
            keyboard = [
                [InlineKeyboardButton("✅ Да, нужен инвойс", callback_data='z_invoice_yes')],
                [InlineKeyboardButton("❌ Нет, без инвойса", callback_data='z_invoice_no')]
            ]
            await update.message.reply_text(
                f'Клиент: <b>{client_name}</b>\n'
                f'В базе заказов не найдено.\n\n'
                f'Нужен инвойс (+10%)?',
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='HTML'
            )
            return Z_INVOICE
    
    await update.message.reply_text(
        'Введи имя клиента:\n'
        'Например: <code>/zakaz Иван Иванов</code>',
        parse_mode='HTML'
    )
    return ConversationHandler.END

async def z_select_order_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = str(update.effective_user.id)
    
    if query.data == 'z_sel_new':
        # Новый заказ — спрашиваем про инвойс
        keyboard = [
            [InlineKeyboardButton("✅ Да, нужен инвойс", callback_data='z_invoice_yes')],
            [InlineKeyboardButton("❌ Нет, без инвойса", callback_data='z_invoice_no')]
        ]
        await query.edit_message_text(
            f'➕ Новый заказ для <b>{orders[uid]["client"]}</b>\n\n'
            f'Нужен инвойс (+10%)?',
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
        return Z_INVOICE
    
    # Выбрали существующий заказ
    try:
        order_idx = int(query.data.replace('z_sel_', ''))
        orders[uid]['selected_order_idx'] = order_idx
        
        order = orders[uid]['all_client_orders'][order_idx]
        items = order.get('items', [])
        
        # Формируем описание товаров для заголовка (без количества)
        if items:
            items_summary = ", ".join([i['name'] for i in items[:3] if i.get('name')])
            if len(items) > 3:
                items_summary += f" +{len(items)-3}"
        else:
            items_summary = order.get('items_text', 'Товар')[:40]
        
        items_text = "\n".join([f"• {i['name']} × {i['qty']}" for i in items if i['name']])
        
        keyboard = [
            [InlineKeyboardButton("📝 Использовать как шаблон", callback_data='z_act_template')],
            [InlineKeyboardButton("✏️ Редактировать количество", callback_data='z_act_edit')],
            [InlineKeyboardButton("🔢 Выбрать конкретные товары", callback_data='z_act_select')],
        ]
        
        await query.edit_message_text(
            f'📦 {items_summary}\n\n'
            f'Товары ({len(items)} шт):\n{items_text}\n\n'
            f'Что сделать?',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return Z_ORDER_ACTION
    except Exception as e:
        logger.error(f"Ошибка в z_select_order_cb: {e}")
        logger.error(traceback.format_exc())
        await query.edit_message_text(f'❌ Ошибка: {str(e)[:100]}\nНачни заново /zakaz')
        return ConversationHandler.END

async def z_order_action_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = str(update.effective_user.id)
    action = query.data
    
    try:
        order_idx = orders[uid]['selected_order_idx']
        order = orders[uid]['all_client_orders'][order_idx]
        
        # Подгружаем курсы
        if order.get('client_rate'):
            orders[uid]['client_rate'] = order['client_rate']
        if order.get('real_rate'):
            orders[uid]['real_rate'] = order['real_rate']
        if order.get('rub_rate'):
            orders[uid]['rub_rate'] = order['rub_rate']
        
        items = order.get('items', [])
        
        if action == 'z_act_template':
            # Использовать как шаблон — спрашиваем про инвойс
            keyboard = [
                [InlineKeyboardButton("✅ Да, нужен инвойс", callback_data='z_invoice_yes')],
                [InlineKeyboardButton("❌ Нет, без инвойса", callback_data='z_invoice_no')]
            ]
            rates_text = []
            if orders[uid].get('client_rate'):
                rates_text.append(f"клиент {orders[uid]['client_rate']}")
            if orders[uid].get('real_rate'):
                rates_text.append(f"реальный {orders[uid]['real_rate']}")
            
            await query.edit_message_text(
                f'📝 Шаблон: {len(items)} товаров\n'
                f'Курсы: {", ".join(rates_text) if rates_text else "нет"}\n\n'
                f'Нужен инвойс (+10%)?',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return Z_INVOICE
        
        elif action == 'z_act_edit':
            orders[uid]['edit_items'] = items.copy()
            orders[uid]['edit_idx'] = 0
            return await show_edit_item(query, uid)
        
        elif action == 'z_act_select':
            keyboard = []
            for idx, item in enumerate(items):
                name = item['name'][:30]
                qty = item.get('qty', 0)
                keyboard.append([InlineKeyboardButton(
                    f"☐ {name} × {qty}", 
                    callback_data=f'z_item_toggle_{idx}'
                )])
            keyboard.append([InlineKeyboardButton("✅ Готово", callback_data='z_items_done')])
            keyboard.append([InlineKeyboardButton("← Назад", callback_data='z_items_back')])
            
            orders[uid]['selected_items'] = set()
            orders[uid]['all_items'] = items
            
            items_list_text = "\n".join([f"• {i['name']} × {i.get('qty', 0)}" for i in items])
            
            await query.edit_message_text(
                f'🔢 Выбери товары (нажми для выбора):\n\n'
                f'В заказе ({len(items)} шт):\n{items_list_text}\n\n'
                f'Выбрано: 0',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return Z_SELECT_ITEMS
    except Exception as e:
        logger.error(f"Ошибка в z_order_action_cb: {e}")
        logger.error(traceback.format_exc())
        await query.edit_message_text(f'❌ Ошибка: {str(e)[:100]}\nНачни заново /zakaz')
        return ConversationHandler.END

async def z_invoice_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора инвойса"""
    query = update.callback_query
    await query.answer()
    uid = str(update.effective_user.id)
    
    try:
        if query.data == 'z_invoice_yes':
            orders[uid]['invoice_needed'] = True
        else:
            orders[uid]['invoice_needed'] = False
        
        await query.edit_message_text('Название товара:')
        return Z_NAME
    except Exception as e:
        logger.error(f"Ошибка в z_invoice_cb: {e}")
        await query.edit_message_text(f'❌ Ошибка: {str(e)[:100]}')
        return ConversationHandler.END

async def show_edit_item(update_or_query, uid):
    """Показываем товар для редактирования количества"""
    try:
        idx = orders[uid]['edit_idx']
        items = orders[uid]['edit_items']
        
        if idx >= len(items):
            orders[uid]['items'] = [{
                'name': i['name'],
                'qty': i['qty'],
                'price': 0,
                'purchase': 0,
                'delivery_factory': 0,
                'dimensions': '',
                'dims': (0, 0, 0),
                'boxes': 1,
                'is_bundle': i.get('is_bundle', False)
            } for i in items if i['qty'] > 0]
            
            if not orders[uid]['items']:
                msg = 'Нет товаров с количеством > 0. Начни сначала /zakaz'
                if hasattr(update_or_query, 'edit_message_text'):
                    await update_or_query.edit_message_text(msg)
                else:
                    await update_or_query.message.reply_text(msg)
                return ConversationHandler.END
            
            msg = (
                f'✏️ Отредактировано: {len(orders[uid]["items"])} товаров\n\n'
                f'Название: {orders[uid]["items"][0]["name"]}\n'
                f'Количество: {orders[uid]["items"][0]["qty"]}\n\n'
                f'Цена клиенту за 1 шт (CNY):'
            )
            if hasattr(update_or_query, 'edit_message_text'):
                await update_or_query.edit_message_text(msg)
            else:
                await update_or_query.message.reply_text(msg)
            orders[uid]['current'] = orders[uid]['items'][0]
            orders[uid]['item_idx'] = 0
            return Z_PRICE
        
        item = items[idx]
        msg = (
            f'✏️ Товар {idx+1}/{len(items)}: <b>{item["name"]}</b>\n'
            f'Текущее количество: {item["qty"]}\n\n'
            f'Введи новое количество (или 0 чтобы убрать):'
        )
        if hasattr(update_or_query, 'edit_message_text'):
            await update_or_query.edit_message_text(msg, parse_mode='HTML')
        else:
            await update_or_query.message.reply_text(msg, parse_mode='HTML')
        return Z_EDIT_ITEM_QTY
    except Exception as e:
        logger.error(f"Ошибка в show_edit_item: {e}")
        msg = f'❌ Ошибка: {str(e)[:100]}'
        if hasattr(update_or_query, 'edit_message_text'):
            await update_or_query.edit_message_text(msg)
        else:
            await update_or_query.message.reply_text(msg)
        return ConversationHandler.END

async def z_edit_item_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try:
        new_qty = int(update.message.text)
        idx = orders[uid]['edit_idx']
        orders[uid]['edit_items'][idx]['qty'] = new_qty
        orders[uid]['edit_idx'] += 1
        return await show_edit_item(update, uid)
    except ValueError:
        await update.message.reply_text('Число! Введи количество:')
        return Z_EDIT_ITEM_QTY
    except Exception as e:
        logger.error(f"Ошибка в z_edit_item_qty: {e}")
        await update.message.reply_text(f'❌ Ошибка: {str(e)[:100]}')
        return ConversationHandler.END

async def z_item_toggle_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = str(update.effective_user.id)
    
    try:
        if query.data == 'z_items_back':
            # Возвращаемся к выбору действия
            order_idx = orders[uid]['selected_order_idx']
            order = orders[uid]['all_client_orders'][order_idx]
            date_str = order.get('date', '??')
            items = order.get('items', [])
            
            items_text = "\n".join([f"• {i['name']} × {i.get('qty', 0)}" for i in items if i['name']])
            
            keyboard = [
                [InlineKeyboardButton("📝 Использовать как шаблон", callback_data='z_act_template')],
                [InlineKeyboardButton("✏️ Редактировать количество", callback_data='z_act_edit')],
                [InlineKeyboardButton("🔢 Выбрать конкретные товары", callback_data='z_act_select')],
            ]
            
            await query.edit_message_text(
                f'📦 Заказ от {date_str}\n\n'
                f'Товары ({len(items)} шт):\n{items_text}\n\n'
                f'Что сделать?',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return Z_ORDER_ACTION
        
        if query.data == 'z_items_done':
            selected = orders[uid]['selected_items']
            all_items = orders[uid]['all_items']
            
            orders[uid]['items'] = [{
                'name': all_items[i]['name'],
                'qty': all_items[i].get('qty', 0),
                'price': 0,
                'purchase': 0,
                'delivery_factory': 0,
                'dimensions': '',
                'dims': (0, 0, 0),
                'boxes': 1,
                'is_bundle': all_items[i].get('is_bundle', False)
            } for i in selected]
            
            if not orders[uid]['items']:
                await query.edit_message_text('Ничего не выбрано. Начни сначала /zakaz')
                return ConversationHandler.END
            
            await query.edit_message_text(
                f'🔢 Выбрано товаров: {len(orders[uid]["items"])}\n\n'
                f'Название: {orders[uid]["items"][0]["name"]}\n'
                f'Количество: {orders[uid]["items"][0]["qty"]}\n\n'
                f'Цена клиенту за 1 шт (CNY):'
            )
            orders[uid]['current'] = orders[uid]['items'][0]
            orders[uid]['item_idx'] = 0
            return Z_PRICE
        
        idx = int(query.data.replace('z_item_toggle_', ''))
        selected = orders[uid]['selected_items']
        all_items = orders[uid]['all_items']
        
        if idx in selected:
            selected.remove(idx)
        else:
            selected.add(idx)
        
        keyboard = []
        for i, item in enumerate(all_items):
            name = item['name'][:30]
            qty = item.get('qty', 0)
            mark = "☑️" if i in selected else "☐"
            keyboard.append([InlineKeyboardButton(
                f"{mark} {name} × {qty}", 
                callback_data=f'z_item_toggle_{i}'
            )])
        keyboard.append([InlineKeyboardButton("✅ Готово", callback_data='z_items_done')])
        keyboard.append([InlineKeyboardButton("← Назад", callback_data='z_items_back')])
        
        selected_names = [all_items[i]['name'] for i in selected]
        selected_text = "\n".join([f"• {n}" for n in selected_names]) if selected_names else "(ничего не выбрано)"
        
        await query.edit_message_text(
            f'🔢 Выбери товары (нажми для выбора):\n\n'
            f'<b>Выбрано ({len(selected)} шт):</b>\n{selected_text}',
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
        return Z_SELECT_ITEMS
    except Exception as e:
        logger.error(f"Ошибка в z_item_toggle_cb: {e}")
        await query.edit_message_text(f'❌ Ошибка: {str(e)[:100]}')
        return ConversationHandler.END

async def z_get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    orders[uid]['current'] = {'name': update.message.text.strip()}
    await update.message.reply_text('Количество:')
    return Z_QTY

async def z_get_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try:
        orders[uid]['current']['qty'] = int(update.message.text)
        await update.message.reply_text('Цена клиенту за 1 шт (CNY):')
        return Z_PRICE
    except:
        await update.message.reply_text('Число! Количество:')
        return Z_QTY

async def z_get_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try:
        orders[uid]['current']['price'] = float(update.message.text)
        await update.message.reply_text('Закупка у фабрики за 1 шт (CNY):')
        return Z_PURCHASE
    except:
        await update.message.reply_text('Число!')
        return Z_PRICE

async def z_get_purchase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try:
        orders[uid]['current']['purchase'] = float(update.message.text)
        await update.message.reply_text('Доставка фабрика→твой склад (CNY):')
        return Z_DELIVERY
    except:
        await update.message.reply_text('Число!')
        return Z_PURCHASE

async def z_get_delivery(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try:
        orders[uid]['current']['delivery_factory'] = float(update.message.text)
        
        # Показываем существующие наборы в заказе
        existing_bundles = list(set(
            item.get('bundle_name') for item in orders[uid].get('items', [])
            if item.get('bundle_name')
        ))
        
        keyboard = []
        
        # Если есть активный набор, показываем его первым с пометкой
        active_bundle = orders[uid].get('active_bundle')
        if active_bundle and active_bundle in existing_bundles:
            keyboard.append([InlineKeyboardButton(f"🔄 Продолжить: {active_bundle}", callback_data=f'z_bundle_existing_{active_bundle}')])
            keyboard.append([InlineKeyboardButton("❌ Выйти из набора", callback_data='z_bundle_exit')])
        
        # Остальные существующие наборы (кроме активного)
        for bundle in existing_bundles:
            if bundle != active_bundle:
                keyboard.append([InlineKeyboardButton(f"📦 {bundle}", callback_data=f'z_bundle_existing_{bundle}')])
        
        # Основные кнопки
        keyboard.append([InlineKeyboardButton("📦 По одиночке", callback_data='z_bundle_single')])
        keyboard.append([InlineKeyboardButton("📦➡️📦 Сделать набором из этого товара", callback_data='z_bundle_make_from_current')])
        keyboard.append([InlineKeyboardButton("➕ Новый пустой набор", callback_data='z_bundle_new')])
        
        msg = "📦 Что это за товар?\n\n"
        if active_bundle:
            msg += f"Сейчас добавляем в: <b>{active_bundle}</b>\n\n"
        elif existing_bundles:
            msg += "Существующие наборы в заказе:\n"
            for b in existing_bundles:
                msg += f"• {b}\n"
            msg += "\n"
        msg += "Выбери: добавить в набор или по одиночке?"
        
        await update.message.reply_text(
            msg,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
        return Z_BUNDLE_SELECT
    except:
        await update.message.reply_text('Число!')
        return Z_DELIVERY

async def z_bundle_select_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора набора"""
    query = update.callback_query
    await query.answer()
    uid = str(update.effective_user.id)
    
    try:
        data = query.data
        
        if data == 'z_bundle_single':
            # Обычный товар
            orders[uid]['current']['bundle_name'] = None
            orders[uid]['current']['is_bundle'] = False
            # Сбрасываем активный набор
            if 'active_bundle' in orders[uid]:
                del orders[uid]['active_bundle']
            await query.edit_message_text(
                '📦 Обычный товар\n\n'
                'Введи размеры 1 шт (Д Ш В в см):\n'
                'Например: 15 10 8'
            )
            return Z_DIMS
            
        elif data == 'z_bundle_exit':
            # Выйти из активного набора
            if 'active_bundle' in orders[uid]:
                del orders[uid]['active_bundle']
            orders[uid]['current']['bundle_name'] = None
            orders[uid]['current']['is_bundle'] = False
            await query.edit_message_text(
                '📦 Вышли из набора\n\n'
                'Введи размеры 1 шт (Д Ш В в см):\n'
                'Например: 15 10 8'
            )
            return Z_DIMS
            
        elif data == 'z_bundle_make_from_current':
            # Сделать текущий товар частью нового набора
            await query.edit_message_text(
                '📦➡️📦 Сделать набором из этого товара\n\n'
                'Введи имя набора:\n'
                'Например: "Сет розовый", "Комбо A"'
            )
            return Z_BUNDLE_NEW_FROM_CURRENT
            
        elif data == 'z_bundle_new':
            # Новый набор - просим имя
            await query.edit_message_text(
                '➕ Новый набор\n\n'
                'Введи имя набора:\n'
                'Например: "Сет розовый", "Комбо A", "Набор 1"'
            )
            return Z_BUNDLE_NEW
            
        elif data.startswith('z_bundle_existing_'):
            # Добавить в существующий набор
            bundle_name = data.replace('z_bundle_existing_', '')
            orders[uid]['current']['bundle_name'] = bundle_name
            orders[uid]['current']['is_bundle'] = True
            # Запоминаем активный набор для последующих товаров
            orders[uid]['active_bundle'] = bundle_name
            await query.edit_message_text(
                f'📦 Добавляем в набор: <b>{bundle_name}</b>\n\n'
                f'Введи размеры этого товара (Д Ш В в см):\n'
                f'Нужно для расчёта коробок',
                parse_mode='HTML'
            )
            return Z_DIMS
            
    except Exception as e:
        logger.error(f"Ошибка в z_bundle_select_cb: {e}")
        await query.edit_message_text(f'❌ Ошибка: {str(e)[:100]}')
        return ConversationHandler.END

async def z_bundle_new_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Создание нового набора - ввод имени, цена устанавливается в /ff"""
    uid = str(update.effective_user.id)
    try:
        bundle_name = update.message.text.strip()
        if not bundle_name:
            await update.message.reply_text('Имя не может быть пустым. Введи имя набора:')
            return Z_BUNDLE_NEW
            
        # Создаём "пустой" набор как контейнер
        # Цена будет установлена в /ff при расчёте
        orders[uid]['current'] = {
            'name': bundle_name,
            'bundle_name': None,  # Это сам набор, не товар в наборе
            'is_bundle': True,
            'dimensions': 'Набор',
            'dims': (0, 0, 0),
            'qty': 1,
            'price': 0,  # Цена будет в /ff
            'purchase': 0,
            'delivery_factory': 0,
            'items_per_box': 0,
            'boxes': 0
        }
        
        keyboard = [[InlineKeyboardButton("✅ Да", callback_data='z_more_yes'), 
                     InlineKeyboardButton("❌ Нет", callback_data='z_more_no')]]
        await update.message.reply_text(
            f'📦 Новый набор: <b>{bundle_name}</b>\n\n'
            f'Набор создан. Теперь добавь товары в этот набор.\n'
            f'Ещё товар?',
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return Z_MORE
    except Exception as e:
        logger.error(f"Ошибка в z_bundle_new_name: {e}")
        await update.message.reply_text(f'❌ Ошибка: {str(e)[:100]}')
        return ConversationHandler.END

async def z_bundle_new_from_current_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Текущий товар становится первым в новом наборе"""
    uid = str(update.effective_user.id)
    try:
        bundle_name = update.message.text.strip()
        if not bundle_name:
            await update.message.reply_text('Имя не может быть пустым. Введи имя набора:')
            return Z_BUNDLE_NEW_FROM_CURRENT
        
        # Запоминаем активный набор
        orders[uid]['active_bundle'] = bundle_name
        
        # Текущий товар теперь в наборе
        orders[uid]['current']['bundle_name'] = bundle_name
        orders[uid]['current']['is_bundle'] = True
        
        await update.message.reply_text(
            f'📦 Товар добавлен в новый набор: <b>{bundle_name}</b>\n\n'
            f'Введи размеры этого товара (Д Ш В в см):\n'
            f'Нужно для расчёта коробок',
            parse_mode='HTML'
        )
        return Z_DIMS
    except Exception as e:
        logger.error(f"Ошибка в z_bundle_new_from_current_name: {e}")
        await update.message.reply_text(f'❌ Ошибка: {str(e)[:100]}')
        return ConversationHandler.END

async def z_bundle_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Старый обработчик - удаляем/отключаем"""
    pass

async def z_get_dims(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    text = update.message.text.strip()
    logger.info(f"z_get_dims called for uid={uid}, text='{text}'")
    
    # Проверка на пропуск размеров (для услуг, цифровых товаров)
    if text in ['-', '—', 'пропустить', 'skip', 'нет']:
        logger.info(f"z_get_dims: skipping dimensions for uid={uid}")
        
        if uid not in orders:
            logger.error(f"z_get_dims: uid {uid} not in orders")
            await update.message.reply_text('Ошибка: сессия не найдена. Начни сначала: /zakaz')
            return ConversationHandler.END
            
        current = orders[uid].get('current', {})
        
        if 'qty' not in current:
            logger.error(f"z_get_dims: qty not in current")
            await update.message.reply_text('Ошибка: количество не задано. Начни сначала: /zakaz')
            return ConversationHandler.END
        
        # Устанавливаем нулевые размеры
        current['dimensions'] = '—'
        current['dims'] = (0, 0, 0)
        current['items_per_box'] = 0
        current['boxes'] = 0
        
        keyboard = [[InlineKeyboardButton("✅ Да", callback_data='z_more_yes'), 
                     InlineKeyboardButton("❌ Нет", callback_data='z_more_no')]]
        await update.message.reply_text(
            '➕ Ещё товар?',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return Z_MORE
    
    try:
        dims = [float(x) for x in text.split()]
        if len(dims) != 3:
            raise ValueError(f"Expected 3 dimensions, got {len(dims)}")
        l, w, h = dims
        
        # Проверяем что orders[uid] и current существуют
        if uid not in orders:
            logger.error(f"z_get_dims: uid {uid} not in orders")
            await update.message.reply_text('Ошибка: сессия не найдена. Начни сначала: /zakaz')
            return ConversationHandler.END
            
        current = orders[uid].get('current', {})
        logger.info(f"z_get_dims: current={current}")
        
        # Проверяем что qty есть
        if 'qty' not in current:
            logger.error(f"z_get_dims: qty not in current")
            await update.message.reply_text('Ошибка: количество не задано. Начни сначала: /zakaz')
            return ConversationHandler.END
            
        current['dimensions'] = f"{int(l)}×{int(w)}×{int(h)}"
        current['dims'] = (l, w, h)
        qty = current['qty']
        items_per_box, boxes = calculate_boxes(l, w, h, qty)
        
        current['items_per_box'] = items_per_box
        current['boxes'] = boxes
        
        logger.info(f"z_get_dims: success, returning Z_MORE")
        keyboard = [[InlineKeyboardButton("✅ Да", callback_data='z_more_yes'), 
                     InlineKeyboardButton("❌ Нет", callback_data='z_more_no')]]
        await update.message.reply_text(
            f'📐 Размеры: {int(l)}×{int(w)}×{int(h)} см\n'
            f'📦 В короб влезет: ~{items_per_box} шт\n'
            f'📦 Коробок: {boxes}\n\n'
            f'➕ Ещё товар?',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return Z_MORE
    except Exception as e:
        logger.error(f"Ошибка в z_get_dims: {e}")
        await update.message.reply_text('Неверный формат. Введи 3 числа (15 10 8) или "-" чтобы пропустить:')
        return Z_DIMS

async def z_more_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка текстового ввода вместо нажатия кнопки Да/Нет"""
    await update.message.reply_text('Нажми кнопку ✅ Да или ❌ Нет')
    return Z_MORE

async def z_more_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = str(update.effective_user.id)
    logger.info(f"z_more_cb called for uid={uid}, data={query.data}")
    
    try:
        current = orders[uid]['current']
        items = orders[uid]['items']
        
        # Добавляем текущий товар в список (если его ещё нет)
        # Проверяем уникальность по комбинации name + bundle_name
        if current and current.get('name'):
            existing_keys = [(i['name'], i.get('bundle_name')) for i in items]
            current_key = (current['name'], current.get('bundle_name'))
            if current_key not in existing_keys:
                items.append(current)
                # Если это набор (контейнер), запоминаем его имя для следующих товаров
                if current.get('is_bundle') and current.get('bundle_name') is None:
                    orders[uid]['active_bundle'] = current['name']
                    logger.info(f"Set active_bundle to {current['name']}")
        
        if query.data == 'z_more_yes':
            # Новый товар
            active_bundle = orders[uid].get('active_bundle')
            if active_bundle:
                # Если есть активный набор, создаём товар в этом наборе
                orders[uid]['current'] = {
                    'bundle_name': active_bundle,
                    'is_bundle': False
                }
                await query.edit_message_text(
                    f'📦 Добавляем в набор: <b>{active_bundle}</b>\n\n'
                    f'Название товара:',
                    parse_mode='HTML'
                )
            else:
                # Обычный товар без набора
                orders[uid]['current'] = {}
                await query.edit_message_text('Название товара:')
            return Z_NAME
        else:
            # Закончили с товарами — идём к курсам
            # Сбрасываем активный набор
            if 'active_bundle' in orders[uid]:
                del orders[uid]['active_bundle']
            if 'client_rate' in orders[uid]:
                await query.edit_message_text(
                    f'Курс клиенту ¥→драм ({orders[uid]["client_rate"]}):\n(отправь новое число или "ok")'
                )
            else:
                await query.edit_message_text('Курс клиенту ¥→драм (например 58):')
            return Z_CLIENT_RATE
    except Exception as e:
        logger.error(f"Ошибка в z_more_cb: {e}")
        await query.edit_message_text(f'❌ Ошибка: {str(e)[:100]}')
        return ConversationHandler.END

async def z_client_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    text = update.message.text.strip().lower()
    
    try:
        if text and text != 'ok':
            orders[uid]['client_rate'] = float(text)
        
        if 'real_rate' in orders[uid]:
            await update.message.reply_text(f'Курс реальный ¥→драм ({orders[uid]["real_rate"]}):\n(отправь новое число или "ok")')
        else:
            await update.message.reply_text('Курс реальный ¥→драм (например 55):')
        return Z_REAL_RATE
    except:
        await update.message.reply_text('Число или "ok"! Курс клиенту:')
        return Z_CLIENT_RATE

async def z_real_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    text = update.message.text.strip().lower()
    
    try:
        if text and text != 'ok':
            orders[uid]['real_rate'] = float(text)
        
        items = orders[uid]['items']
        client_rate = orders[uid]['client_rate']
        total_purchase = sum(i['purchase'] * i['qty'] for i in items)
        delivery = sum(i.get('delivery_factory', 0) for i in items)
        
        base_amd = int((total_purchase + delivery) * client_rate)
        comm_3 = int(base_amd * 0.03)
        
        if comm_3 < 10000:
            keyboard = [
                [InlineKeyboardButton("10000 драм", callback_data='z_comm_10000')],
                [InlineKeyboardButton("15000 драм", callback_data='z_comm_15000')]
            ]
            await update.message.reply_text(
                f'Комиссия: 3% = {comm_3} драм (меньше 10000)\nВыбери фикс:',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            comm_5 = int(base_amd * 0.05)
            keyboard = [
                [InlineKeyboardButton(f"3% = {comm_3} драм", callback_data='z_comm_3')],
                [InlineKeyboardButton(f"5% = {comm_5} драм", callback_data='z_comm_5')]
            ]
            await update.message.reply_text('Выбери комиссию:', reply_markup=InlineKeyboardMarkup(keyboard))
        return Z_COMMISSION
    except Exception as e:
        logger.error(f"Ошибка в z_real_rate: {e}")
        await update.message.reply_text(f'Ошибка: {str(e)[:100]}')
        return ConversationHandler.END

async def z_commission_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = str(update.effective_user.id)
    data = query.data
    
    try:
        items = orders[uid]['items']
        client_rate = orders[uid]['client_rate']
        real_rate = orders[uid].get('real_rate', client_rate - 3)  # Реальный курс (обычно на 3 меньше)
        total_purchase = sum(i['purchase'] * i['qty'] for i in items)
        delivery = sum(i.get('delivery_factory', 0) for i in items)
        
        base_amd = int((total_purchase + delivery) * client_rate)
        real_amd = int((total_purchase + delivery) * real_rate)
        total_price = sum(i['price'] * i['qty'] for i in items)
        
        if data == 'z_comm_3':
            commission_type = '3%'
        elif data == 'z_comm_5':
            commission_type = '5%'
        elif data == 'z_comm_10000':
            commission_type = '10000'
        elif data == 'z_comm_15000':
            commission_type = '15000'
        
        orders[uid]['commission_type'] = commission_type
        
        # === ПРАВИЛЬНЫЙ РАСЧЁТ ===
        # 1. Товар + Доставка в CNY
        total_cny = total_price + delivery
        
        # 2. Комиссия
        if commission_type == '3%':
            commission_cny = int(total_cny * 0.03)
            commission_amd = int(commission_cny * client_rate)
        elif commission_type == '5%':
            commission_cny = int(total_cny * 0.05)
            commission_amd = int(commission_cny * client_rate)
        elif commission_type == '10000':
            # Фикс 10000 AMD
            commission_amd = 10000
            commission_cny = round(10000 / client_rate, 2)  # Только для отображения
        elif commission_type == '15000':
            # Фикс 15000 AMD
            commission_amd = 15000
            commission_cny = round(15000 / client_rate, 2)
        
        orders[uid]['commission_cny'] = commission_cny
        orders[uid]['commission_amd'] = commission_amd
        
        # 3. Итог в AMD
        # Процент: (total_cny + commission_cny) * rate
        # Фикс: (total_cny * rate) + commission_amd
        if commission_type in ['3%', '5%']:
            total_with_commission_cny = total_cny + commission_cny
            total_amd_client = int(total_with_commission_cny * client_rate)
        else:  # Фиксированная
            total_amd_client = int(total_cny * client_rate) + commission_amd
        
        orders[uid]['total_cny'] = total_cny
        orders[uid]['total_with_commission_cny'] = total_cny + commission_cny
        orders[uid]['total_amd'] = total_amd_client
        
        client_name = orders[uid]['client']
        
        # === ПИСЬМО ДЛЯ КЛИЕНТА (с правильным расчётом) ===
        client_msg = f"📋 <b>Расчёт заказа</b>\n"
        client_msg += f"Клиент: <b>{client_name}</b>\n\n"
        
        # Детализация по каждому товару
        for i in items:
            item_price = i['price']
            item_qty = int(i['qty'])
            item_subtotal = item_price * item_qty
            
            client_msg += f"<b>{i['name']}</b>\n"
            client_msg += f"  {item_qty} × {fmt(item_price)}¥ = {fmt(item_subtotal)}¥\n\n"
        
        client_msg += f"━━━━━━━━━━━━\n"
        client_msg += f"Товар: {fmt(total_price)}¥\n"
        client_msg += f"Доставка: {fmt(delivery)}¥\n"
        client_msg += f"━━━━━━━━━━━━\n"
        client_msg += f"Итого: {fmt(total_cny)}¥\n"
        
        if commission_type in ['3%', '5%']:
            client_msg += f"+ Комиссия ({commission_type}): {fmt(commission_cny)}¥\n"
            client_msg += f"━━━━━━━━━━━━\n"
            client_msg += f"Всего: {fmt(total_cny + commission_cny)}¥\n"
            client_msg += f"× Курс {client_rate} = <b>{total_amd_client:,} AMD</b>\n"
        else:
            # Фиксированная комиссия — показываем в AMD сразу
            subtotal_amd = int(total_cny * client_rate)
            client_msg += f"× Курс {client_rate} = {subtotal_amd:,} AMD\n"
            client_msg += f"+ Комиссия ({commission_type}): {commission_amd:,} AMD\n"
            client_msg += f"━━━━━━━━━━━━\n"
            client_msg += f"<b>К ОПЛАТЕ: {total_amd_client:,} AMD</b>\n"
        
        client_msg += f"━━━━━━━━━━━━"
        
        # === ПИСЬМО ДЛЯ СЕБЯ (детальное) ===
        order_code = get_code(orders[uid]['client'])
        commission_amd = orders[uid].get('commission_amd', int(commission_cny * client_rate))
        
        # Моя закупка (по реальному курсу)
        purchase_amd = int((total_purchase + delivery) * real_rate)
        
        # Я получаю от клиента
        received_amd = total_amd_client
        
        # Моя прибыль
        profit = received_amd - purchase_amd
        
        my_msg = f"💼 <b>МОЙ РАСЧЁТ: {order_code}</b>\n\n"
        my_msg += f"<b>На закупку:</b>\n"
        my_msg += f"  • Товар: {fmt(total_purchase)}¥\n"
        my_msg += f"  • Доставка: {fmt(delivery)}¥\n"
        my_msg += f"  • Итого: {fmt(total_purchase + delivery)}¥ × {real_rate} = {purchase_amd:,} AMD\n\n"
        
        my_msg += f"<b>От клиента ({client_rate}):</b>\n"
        my_msg += f"  • Товар+доставка: {fmt(total_cny)}¥ × {client_rate} = {int(total_cny * client_rate):,} AMD\n"
        
        if commission_type in ['3%', '5%']:
            my_msg += f"  • Комиссия ({commission_type}): {fmt(commission_cny)}¥ × {client_rate} = {commission_amd:,} AMD\n"
        else:
            my_msg += f"  • Комиссия ({commission_type}): {commission_amd:,} AMD\n"
        
        my_msg += f"  • <b>Итого: {received_amd:,} AMD</b>\n\n"
        
        my_msg += f"<b>Прибыль:</b> {received_amd:,} - {purchase_amd:,} = <b>{profit:,} AMD</b>\n"
        
        # Инвойс
        if orders[uid].get('invoice_needed'):
            invoice_amount = int(received_amd * 1.10)
            my_msg += f"\n📄 <b>Инвойс:</b> Да (+10%)\n"
            my_msg += f"💵 <b>Сумма инвойса:</b> {invoice_amount:,} AMD\n"
            my_msg += f"💵 <b>Чистая прибыль:</b> {profit:,} AMD"
        else:
            my_msg += f"\n📄 <b>Инвойс:</b> Нет\n"
            my_msg += f"💵 <b>Чистая прибыль:</b> {profit:,} AMD"
        
        # Отправляем оба сообщения
        await context.bot.send_message(chat_id=update.effective_user.id, text=client_msg, parse_mode='HTML')
        await context.bot.send_message(chat_id=update.effective_user.id, text=my_msg, parse_mode='HTML')
        
        # === АВТО-СОХРАНЕНИЕ В NOTION ===
        if notion and NOTION_DATABASE_ID and not orders[uid].get('notion_error'):
            try:
                notion_url = await save_to_notion(update, context, uid)
                if notion_url:
                    await context.bot.send_message(
                        chat_id=update.effective_user.id,
                        text=f"✅ Сохранено в Notion:\n{notion_url}",
                        parse_mode='HTML'
                    )
                else:
                    # Проверяем, есть ли запись в Notion по коду заказа
                    await context.bot.send_message(
                        chat_id=update.effective_user.id,
                        text="⚠️ Сохранение в Notion не подтверждено. Проверьте базу вручную.",
                        parse_mode='HTML'
                    )
            except Exception as e:
                logger.error(f"Ошибка авто-сохранения в Notion: {e}")
                await context.bot.send_message(
                    chat_id=update.effective_user.id,
                    text=f"⚠️ Ошибка сохранения: {str(e)[:100]}",
                    parse_mode='HTML'
                )
        
        save_session()
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Ошибка в z_commission_cb: {e}")
        logger.error(traceback.format_exc())
        await query.edit_message_text(f'❌ Ошибка: {str(e)[:100]}')
        return ConversationHandler.END

# ======== /FF v38 - НОВАЯ ЛОГИКА НАБОРОВ ========

# Новые состояния для FF
F_SELECT_ORDER, F_MAIN_MENU, F_SINGLE_ITEMS, F_BUNDLE_CREATE, F_BUNDLE_NAME, F_BUNDLE_DIMS, F_BUNDLE_PACKAGE, F_BUNDLE_THERMAL, F_BUNDLE_WORK, F_SUMMARY = range(10)

async def cmd_ff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Новая команда /ff с логикой выбора режима"""
    uid = str(update.effective_user.id)
    
    # Если передано имя клиента — ищем в базе
    if context.args:
        client_name = ' '.join(context.args)
        
        # Получаем заказы клиента
        client_orders, error = await get_client_orders_from_notion(client_name)
        
        if client_orders is None:
            await update.message.reply_text(
                f'⚠️ Notion временно недоступен.\n'
                f'Для нового заказа сначала выполни /zakaz {client_name}'
            )
            return ConversationHandler.END
        
        # Инициализируем сессию
        orders[uid] = {
            'client': client_name,
            'items': [],
            'type': 'ff',
            'all_client_orders': client_orders,
            'ff_bundles': [],  # Список созданных наборов в FF
            'ff_single_items': [],  # Товары, которые считаем по одиночке
            'ff_items_in_bundles': set(),  # Индексы товаров, которые уже в наборах
        }
        
        if client_orders:
            # Показываем список заказов
            keyboard = []
            for idx, order in enumerate(client_orders[:5]):
                items_list = order.get('items', [])
                if items_list:
                    items_names = [i['name'][:12] for i in items_list[:3] if i.get('name')]
                    items_desc = ", ".join(items_names)
                    if len(items_list) > 3:
                        items_desc += f" +{len(items_list)-3}"
                else:
                    items_desc = order.get('items_text', 'Товар')[:25]
                
                btn_text = f"📦 {items_desc}"
                keyboard.append([InlineKeyboardButton(btn_text, callback_data=f'sel_order_{idx}')])
            
            keyboard.append([InlineKeyboardButton("➕ Новый заказ", callback_data='sel_order_new')])
            
            await update.message.reply_text(
                f'📦 FF Китай\nКлиент: <b>{client_name}</b>\n'
                f'Найдено заказов: {len(client_orders)}\n\n'
                f'Выбери заказ или создай новый:',
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='HTML'
            )
            return F_SELECT_ORDER
        else:
            await update.message.reply_text(
                f'Клиент: <b>{client_name}</b>\n'
                f'В базе заказов не найдено.\n\n'
                f'Для нового заказа сначала выполни /zakaz {client_name}'
            )
            return ConversationHandler.END
    
    # Без аргументов — работаем как раньше (нужен предыдущий /zakaz)
    logger.info(f"cmd_ff: uid={uid}, has_orders={uid in orders}, items_count={len(orders.get(uid, {}).get('items', []))}")
    
    if uid not in orders or not orders[uid].get('items'):
        msg = 'Сначала выполни /zakaz [имя клиента]\n\nИли сразу: /ff [имя клиента]'
        if hasattr(update, 'message') and update.message:
            await update.message.reply_text(msg)
        elif hasattr(update, 'callback_query') and update.callback_query:
            await update.callback_query.edit_message_text(msg)
        logger.warning(f"cmd_ff: no data for uid={uid}")
        return ConversationHandler.END
    
    result = await show_order_selection_ff(update, context, uid)
    logger.info(f"cmd_ff: show_order_selection_ff returned {result}")
    if result is None:
        return ConversationHandler.END
    return result

async def show_order_selection_ff(update: Update, context: ContextTypes.DEFAULT_TYPE, uid):
    """Показывает выбор заказа для FF"""
    logger.info(f"show_order_selection_ff: uid={uid}, orders keys={list(orders.get(uid, {}).keys())}")
    
    if uid not in orders:
        msg = 'Сначала выполни /zakaz [имя клиента]'
        if hasattr(update, 'message') and update.message:
            await update.message.reply_text(msg)
        elif hasattr(update, 'callback_query') and update.callback_query:
            await update.callback_query.edit_message_text(msg)
        return None
    
    # Конвертируем наборы из /zakaz в формат /ff
    convert_zakaz_bundles_to_ff(uid)
    
    client_orders = orders[uid].get('all_client_orders', [])
    client = orders[uid].get('client', 'Неизвестно')
    items = orders[uid].get('items', [])
    
    logger.info(f"show_order_selection_ff: client={client}, items={len(items)}, client_orders={len(client_orders)}")
    
    if not client_orders:
        # Нет заказов в базе — работаем с текущим
        if items:
            logger.info(f"show_order_selection_ff: showing main menu with {len(items)} items")
            return await show_ff_main_menu(update, context, uid)
        msg = 'Нет данных для расчёта. Сначала выполни /zakaz'
        if hasattr(update, 'message') and update.message:
            await update.message.reply_text(msg)
        elif hasattr(update, 'callback_query') and update.callback_query:
            await update.callback_query.edit_message_text(msg)
        return None
    
    if len(client_orders) == 1:
        await load_order_data_ff(uid, 0)
        return await show_ff_main_menu(update, context, uid)
    
    keyboard = []
    for idx, order in enumerate(client_orders[:5]):
        items_list = order.get('items', [])
        if items_list:
            items_names = [i['name'][:12] for i in items_list[:3] if i.get('name')]
            items_desc = ", ".join(items_names)
            if len(items_list) > 3:
                items_desc += f" +{len(items_list)-3}"
        else:
            items_desc = order.get('items_text', 'Товар')[:25]
        
        btn_text = f"📦 {items_desc}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f'sel_order_{idx}')])
    
    keyboard.append([InlineKeyboardButton("➕ Новый заказ", callback_data='sel_order_new')])
    
    msg_text = f'📦 FF Китай\nКлиент: <b>{client}</b>\n\nВыбери заказ:'
    
    if hasattr(update, 'message') and update.message:
        await update.message.reply_text(
            msg_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
    elif hasattr(update, 'callback_query') and update.callback_query:
        await update.callback_query.edit_message_text(
            msg_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
    return F_SELECT_ORDER

async def load_order_data_ff(uid, order_idx):
    """Загружает данные заказа для FF"""
    client_orders = orders[uid].get('all_client_orders', [])
    if 0 <= order_idx < len(client_orders):
        order = client_orders[order_idx]
        if order.get('client_rate'):
            orders[uid]['client_rate'] = order['client_rate']
        if order.get('real_rate'):
            orders[uid]['real_rate'] = order['real_rate']
        if order.get('rub_rate'):
            orders[uid]['rub_rate'] = order['rub_rate']
        orders[uid]['selected_order_date'] = order.get('date', '')
        orders[uid]['notion_page_id'] = order.get('id')
        # Загружаем товары из заказа
        if order.get('items'):
            orders[uid]['items'] = [{
                'name': i['name'],
                'qty': i.get('qty', 0),
                'price': 0,
                'purchase': 0,
                'delivery_factory': 0,
                'dimensions': i.get('dimensions', ''),
                'dims': i.get('dims', (0, 0, 0)),
                'boxes': 1,
                'is_bundle': i.get('is_bundle', False)
            } for i in order['items'] if i.get('name')]
        # Инициализируем FF-специфичные поля
        orders[uid]['ff_bundles'] = []
        orders[uid]['ff_single_items'] = []
        orders[uid]['ff_items_in_bundles'] = set()

async def convert_zakaz_bundles_to_ff(uid):
    """Конвертирует наборы из /zakaz (bundle_name) в формат /ff (ff_bundles)"""
    items = orders[uid].get('items', [])
    if not items:
        return
    
    # Группируем товары по bundle_name
    bundles_map = {}
    for idx, item in enumerate(items):
        bundle_name = item.get('bundle_name')
        if bundle_name:
            if bundle_name not in bundles_map:
                bundles_map[bundle_name] = []
            bundles_map[bundle_name].append(idx)
    
    # Создаём ff_bundles из сгруппированных товаров
    ff_bundles = []
    items_in_bundles = set()
    
    for bundle_name, item_indices in bundles_map.items():
        # Считаем общие размеры набора (пока просто сумма)
        total_qty = sum(items[i]['qty'] for i in item_indices)
        
        ff_bundles.append({
            'name': bundle_name,
            'item_indices': item_indices,
            'dims': (0, 0, 0),  # Будет запрошено в /ff
            'packages': [],
            'total_qty': total_qty,
            'source': 'zakaz'  # Помечаем что из /zakaz
        })
        items_in_bundles.update(item_indices)
    
    orders[uid]['ff_bundles'] = ff_bundles
    orders[uid]['ff_items_in_bundles'] = items_in_bundles
    logger.info(f"Converted {len(ff_bundles)} bundles from /zakaz for uid={uid}")

async def f_select_order_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора заказа в FF"""
    query = update.callback_query
    await query.answer()
    uid = str(update.effective_user.id)
    
    if query.data == 'sel_order_new':
        await query.edit_message_text('Для нового заказа сначала выполни /zakaz [имя]')
        return ConversationHandler.END
    
    try:
        order_idx = int(query.data.replace('sel_order_', ''))
        await load_order_data_ff(uid, order_idx)
        
        # Конвертируем наборы из /zakaz в формат /ff
        await convert_zakaz_bundles_to_ff(uid)
        
        order = orders[uid]['all_client_orders'][order_idx]
        items_list = order.get('items', [])
        if items_list:
            items_summary = ", ".join([i['name'] for i in items_list[:3] if i.get('name')])
            if len(items_list) > 3:
                items_summary += f" +{len(items_list)-3}"
        else:
            items_summary = order.get('items_text', 'Товар')[:40]
        
        await query.edit_message_text(f'📦 FF: {items_summary}')
        return await show_ff_main_menu(update, context, uid)
    except Exception as e:
        logger.error(f"Ошибка в f_select_order_cb: {e}")
        await query.edit_message_text(f'❌ Ошибка: {str(e)[:100]}')
        return ConversationHandler.END

async def show_ff_main_menu(update_or_query, context, uid):
    """Главное меню FF — выбор режима работы с товарами"""
    items = orders[uid].get('items', [])
    
    if not items:
        msg = 'Нет товаров для расчёта FF'
        if hasattr(update_or_query, 'edit_message_text'):
            await update_or_query.edit_message_text(msg)
        elif hasattr(update_or_query, 'message') and update_or_query.message:
            await update_or_query.message.reply_text(msg)
        elif hasattr(update_or_query, 'effective_message') and update_or_query.effective_message:
            await update_or_query.effective_message.reply_text(msg)
        return ConversationHandler.END
    
    # Формируем список товаров с чекбоксами
    items_in_bundles = orders[uid].get('ff_items_in_bundles', set())
    bundles = orders[uid].get('ff_bundles', [])
    
    msg = "📦 <b>FF Китай — Выбор режима</b>\n\n"
    msg += "<b>Товары:</b>\n"
    
    for idx, item in enumerate(items):
        if idx in items_in_bundles:
            # Находим в каком наборе этот товар
            bundle_name = None
            for b in bundles:
                if idx in b.get('item_indices', []):
                    bundle_name = b.get('name', 'Набор')
                    break
            msg += f"☑️ <s>{item['name']}</s> (в наборе \"{bundle_name}\")\n"
        else:
            msg += f"☐ {item['name']}\n"
    
    msg += f"\n<b>Создано наборов:</b> {len(bundles)}\n"
    for b in bundles:
        msg += f"  📦 {b.get('name', 'Без имени')}\n"
    
    # Кнопки режимов
    keyboard = [
        [InlineKeyboardButton("📦 Считать по одиночке", callback_data='ff_mode_single')],
        [InlineKeyboardButton("📦 Собрать набор", callback_data='ff_mode_bundle')],
        [InlineKeyboardButton("✅ Продолжить →", callback_data='ff_mode_continue')],
    ]
    
    if hasattr(update_or_query, 'edit_message_text'):
        await update_or_query.edit_message_text(
            msg, 
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
    elif hasattr(update_or_query, 'message') and update_or_query.message:
        await update_or_query.message.reply_text(
            msg,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
    elif hasattr(update_or_query, 'effective_message') and update_or_query.effective_message:
        await update_or_query.effective_message.reply_text(
            msg,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
    return F_MAIN_MENU

async def ff_main_menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора режима в главном меню FF"""
    query = update.callback_query
    await query.answer()
    uid = str(update.effective_user.id)
    data = query.data
    
    logger.info(f"FF main menu: user={uid}, data={data}")
    
    try:
        items = orders[uid].get('items', [])
        logger.info(f"FF main menu: items count={len(items)}")
        for i, it in enumerate(items[:3]):
            logger.info(f"  Item {i}: {it.get('name')} dims={it.get('dims')}")
        
        # Проверяем есть ли товары без размеров
        items_no_dims = [i for i in items if i.get('dims', (0,0,0)) == (0,0,0)]
        if items_no_dims:
            logger.warning(f"Items without dims: {[i.get('name') for i in items_no_dims]}")
        
        if data == 'ff_mode_single':
            # Считать по одиночке — показываем товары, которые не в наборах
            return await start_single_items(update, context, uid)
        
        elif data == 'ff_mode_bundle':
            # Собрать набор — показываем чекбоксы для выбора товаров
            return await start_bundle_creation(update, context, uid)
        
        elif data == 'ff_mode_continue':
            # Продолжить — считаем итог
            return await calculate_ff_summary(update, context, uid)
        
        elif data == 'ff_back_menu':
            # Вернуться в меню
            return await show_ff_main_menu(update, context, uid)
            
    except Exception as e:
        logger.error(f"Ошибка в ff_main_menu_cb: {e}")
        logger.error(traceback.format_exc())
        await query.edit_message_text(f'❌ Ошибка: {str(e)[:200]}')
        return ConversationHandler.END

async def start_single_items(update_or_query, context, uid):
    """Начинаем расчёт товаров по одиночке"""
    items = orders[uid].get('items', [])
    items_in_bundles = orders[uid].get('ff_items_in_bundles', set())
    
    # Фильтруем товары, которые не в наборах
    available_items = [(idx, item) for idx, item in enumerate(items) if idx not in items_in_bundles]
    
    if not available_items:
        msg = "⚠️ Нет товаров для расчёта по одиночке. Все товары уже в наборах."
        if hasattr(update_or_query, 'edit_message_text'):
            await update_or_query.edit_message_text(msg)
        else:
            await update_or_query.message.reply_text(msg)
        return await show_ff_main_menu(update_or_query, context, uid)
    
    # Инициализируем данные для одиночных товаров
    orders[uid]['ff_single_items'] = []
    orders[uid]['ff_single_index'] = 0
    orders[uid]['ff_single_available'] = available_items
    
    return await show_single_item(update_or_query, context, uid)

async def show_single_item(update_or_query, context, uid):
    """Показываем текущий товар для выбора пакета"""
    idx = orders[uid]['ff_single_index']
    available = orders[uid]['ff_single_available']
    
    if idx >= len(available):
        # Закончили с одиночными товарами, возвращаемся в меню
        return await show_ff_main_menu(update_or_query, context, uid)
    
    item_idx, item = available[idx]
    l, w, h = item['dims']
    qty = item['qty']
    
    # Проверяем есть ли размеры
    if (l, w, h) == (0, 0, 0):
        msg = f"⚠️ Товар <b>{item['name']}</b> не имеет размеров в Notion.\n\n"
        msg += f"Возможные причины:\n"
        msg += f"• В Notion пустое поле 'Размеры (Д×Ш×В)'\n"
        msg += f"• Неверный формат размеров\n\n"
        msg += f"Используй /zakaz для пересоздания заказа с размерами."
        
        if hasattr(update_or_query, 'edit_message_text'):
            await update_or_query.edit_message_text(msg, parse_mode='HTML')
        else:
            await update_or_query.message.reply_text(msg, parse_mode='HTML')
        return F_SINGLE_ITEMS
    
    # Получаем пакеты из Notion
    packages = await get_packages_from_notion()
    orders[uid]['ff_available_packages'] = packages
    
    msg = f"📦 Товар {idx+1}/{len(available)}: <b>{item['name']}</b>\n"
    msg += f"📐 Размеры: {int(l)}×{int(w)}×{int(h)} см | Кол-во: {qty} шт\n\n"
    msg += f"<b>Выбери пакет:</b>"
    
    keyboard = []
    for pkg_idx, pkg in enumerate(packages):
        btn_text = f"📦 {pkg['name']} — {pkg['price']}¥ ({int(pkg['l'])}×{int(pkg['w'])}×{int(pkg['h'])}см)"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f'ff_single_pkg_{pkg_idx}')])
    
    keyboard.append([InlineKeyboardButton("💰 Своя цена", callback_data='ff_single_custom')])
    keyboard.append([InlineKeyboardButton("← Назад в меню", callback_data='ff_back_menu')])
    
    if hasattr(update_or_query, 'edit_message_text'):
        await update_or_query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    else:
        await update_or_query.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    return F_SINGLE_ITEMS

async def ff_single_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора пакета для одиночного товара"""
    query = update.callback_query
    await query.answer()
    uid = str(update.effective_user.id)
    data = query.data
    
    try:
        if data.startswith('ff_single_pkg_'):
            pkg_idx = int(data.replace('ff_single_pkg_', ''))
            packages = orders[uid].get('ff_available_packages', [])
            
            if pkg_idx < len(packages):
                selected_pkg = packages[pkg_idx]
                idx = orders[uid]['ff_single_index']
                available = orders[uid]['ff_single_available']
                item_idx, item = available[idx]
                qty = item['qty']
                
                pkg_total = selected_pkg['price'] * qty
                
                # Сохраняем данные
                orders[uid]['ff_single_items'].append({
                    'item_idx': item_idx,
                    'item': item,
                    'pkg': selected_pkg,
                    'total': pkg_total,
                    'qty': qty
                })
                
                await query.edit_message_text(
                    f"✅ <b>{item['name']}</b>\n"
                    f"   {selected_pkg['name']} × {qty} = {fmt(pkg_total)}¥"
                )
                
                orders[uid]['ff_single_index'] += 1
                return await show_single_item(update, context, uid)
        
        elif data == 'ff_single_custom':
            await query.edit_message_text('Введи цену пакетов (¥):')
            return F_SINGLE_ITEMS
            
    except Exception as e:
        logger.error(f"Ошибка в ff_single_cb: {e}")
        await query.edit_message_text(f'❌ Ошибка: {str(e)[:100]}')
        return ConversationHandler.END

async def ff_single_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ручной ввод цены пакета для одиночного товара"""
    uid = str(update.effective_user.id)
    try:
        price = float(update.message.text)
        idx = orders[uid]['ff_single_index']
        available = orders[uid]['ff_single_available']
        item_idx, item = available[idx]
        qty = item['qty']
        
        total_price = price * qty
        
        orders[uid]['ff_single_items'].append({
            'item_idx': item_idx,
            'item': item,
            'pkg': {'name': 'Пакет (ручной)'},
            'total': total_price,
            'qty': qty
        })
        
        orders[uid]['ff_single_index'] += 1
        return await show_single_item(update, context, uid)
    except:
        await update.message.reply_text('Число! Введи цену:')
        return F_SINGLE_ITEMS

async def start_bundle_creation(update_or_query, context, uid):
    """Начинаем создание набора — выб�
