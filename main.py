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
    """Расчёт количества коробок с учётом макс. размеров"""
    MAX_L, MAX_W, MAX_H = 60, 40, 40  # ИСПРАВЛЕНО: было 60,50,40
    
    items_per_box_l = max(1, int(MAX_L // l))
    items_per_box_w = max(1, int(MAX_W // w))
    items_per_box_h = max(1, int(MAX_H // h))
    items_per_box = items_per_box_l * items_per_box_w * items_per_box_h
    
    boxes = math.ceil(qty / items_per_box)
    return items_per_box, boxes

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
    """Получаем все заказы клиента из Notion с обработкой ошибок (case-insensitive)"""
    if not notion or not NOTION_DATABASE_ID:
        return None, "Notion не настроен"
    
    try:
        # Получаем больше заказов и фильтруем вручную (case-insensitive)
        res = notion.databases.query(
            database_id=NOTION_DATABASE_ID,
            sorts=[{"timestamp": "created_time", "direction": "descending"}],
            page_size=100
        )
        
        orders_list = []
        client_name_lower = client_name.lower()
        
        for page in res.get('results', []):
            props = page['properties']
            # Получаем имя клиента из заказа
            order_client = props.get('Клиент', {}).get('select', {}).get('name', '')
            
            # Case-insensitive сравнение
            if order_client and order_client.lower() == client_name_lower:
                created = page.get('created_time', '')[:10]
                items_text = props.get('Описание товара', {}).get('rich_text', [{}])[0].get('text', {}).get('content', '')
                items_list = []
                seen_names = set()  # Для дедупликации
                for item_str in items_text.split(';'):
                    item_str = item_str.strip()
                    if item_str:
                        # Пробуем разные форматы: "Name × 100", "Name x 100", "Name 100"
                        name = item_str
                        qty = 0
                        
                        # Ищем × (multiplication sign)
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
                
                # Получаем размеры из Notion
                dimensions_str = props.get('Размеры (Д×Ш×В)', {}).get('rich_text', [{}])[0].get('text', {}).get('content', '')
                dims = (0, 0, 0)
                if dimensions_str and '×' in dimensions_str:
                    try:
                        dim_parts = dimensions_str.split('×')
                        if len(dim_parts) == 3:
                            dims = (float(dim_parts[0]), float(dim_parts[1]), float(dim_parts[2]))
                    except:
                        dims = (0, 0, 0)
                
                # Добавляем размеры к каждому товару
                for item in items_list:
                    item['dimensions'] = dimensions_str
                    item['dims'] = dims
                
                order = {
                    'id': page['id'],
                    'code': props.get('Код заказа', {}).get('title', [{}])[0].get('text', {}).get('content', ''),
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
        error_str = str(e)
        error_msg = f"{type(e).__name__}: {error_str}"
        logger.error(f"Error fetching client orders: {error_msg}")
        return None, error_msg
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
        f"🤖 <b>GS Orders Bot</b>\n"
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

Z_INVOICE, Z_SELECT_ORDER, Z_ORDER_ACTION, Z_SELECT_ITEMS, Z_EDIT_ITEM_QTY, Z_NAME, Z_QTY, Z_PRICE, Z_PURCHASE, Z_DELIVERY, Z_BUNDLE_SELECT, Z_BUNDLE_NEW, Z_DIMS, Z_MORE, Z_CLIENT_RATE, Z_REAL_RATE, Z_COMMISSION = range(17)

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
        
        # Кнопки существующих наборов
        for bundle in existing_bundles:
            keyboard.append([InlineKeyboardButton(f"📦 {bundle}", callback_data=f'z_bundle_existing_{bundle}')])
        
        # Основные кнопки
        keyboard.append([InlineKeyboardButton("➕ Новый набор", callback_data='z_bundle_new')])
        keyboard.append([InlineKeyboardButton("📦 По одиночке", callback_data='z_bundle_single')])
        
        msg = "📦 Что это за товар?\n\n"
        if existing_bundles:
            msg += "Существующие наборы в заказе:\n"
            for b in existing_bundles:
                msg += f"• {b}\n"
            msg += "\n"
        msg += "Выбери: добавить в набор или по одиночке?"
        
        await update.message.reply_text(
            msg,
            reply_markup=InlineKeyboardMarkup(keyboard)
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
            await query.edit_message_text(
                '📦 Обычный товар\n\n'
                'Введи размеры 1 шт (Д Ш В в см):\n'
                'Например: 15 10 8'
            )
            return Z_DIMS
            
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
    """Создание нового набора - ввод имени"""
    uid = str(update.effective_user.id)
    try:
        bundle_name = update.message.text.strip()
        if not bundle_name:
            await update.message.reply_text('Имя не может быть пустым. Введи имя набора:')
            return Z_BUNDLE_NEW
            
        orders[uid]['current']['bundle_name'] = bundle_name
        orders[uid]['current']['is_bundle'] = True
        await update.message.reply_text(
            f'📦 Новый набор: <b>{bundle_name}</b>\n\n'
            f'Введи размеры упаковки для этого набора (Д Ш В в см):\n'
            f'Эти размеры будут использоваться в /ff для выбора пакета',
            parse_mode='HTML'
        )
        return Z_DIMS
    except Exception as e:
        logger.error(f"Ошибка в z_bundle_new_name: {e}")
        await update.message.reply_text(f'❌ Ошибка: {str(e)[:100]}')
        return ConversationHandler.END

async def z_bundle_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Старый обработчик - удаляем/отключаем"""
    pass

async def z_get_dims(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    text = update.message.text.strip()
    try:
        dims = [float(x) for x in text.split()]
        if len(dims) != 3:
            raise ValueError
        l, w, h = dims
        current = orders[uid]['current']
        current['dimensions'] = f"{int(l)}×{int(w)}×{int(h)}"
        current['dims'] = (l, w, h)
        qty = current['qty']
        items_per_box, boxes = calculate_boxes(l, w, h, qty)
        
        current['items_per_box'] = items_per_box
        current['boxes'] = boxes
        
        keyboard = [[InlineKeyboardButton("Да", callback_data='z_more_yes'), 
                     InlineKeyboardButton("Нет", callback_data='z_more_no')]]
        await update.message.reply_text(
            f'📐 Размеры: {int(l)}×{int(w)}×{int(h)} см\n'
            f'📦 В короб влезет: ~{items_per_box} шт\n'
            f'📦 Коробок: {boxes}\n\n'
            f'Ещё товар?',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return Z_MORE
    except:
        await update.message.reply_text('Неверный формат. Введи 3 числа:\n15 10 8')
        return Z_DIMS

async def z_more_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = str(update.effective_user.id)
    
    try:
        current = orders[uid]['current']
        items = orders[uid]['items']
        
        # Добавляем текущий товар в список (если его ещё нет)
        if current and current.get('name'):
            existing_names = [i['name'] for i in items]
            if current['name'] not in existing_names:
                items.append(current)
        
        if query.data == 'z_more_yes':
            # Новый товар
            orders[uid]['current'] = {}
            await query.edit_message_text('Название товара:')
            return Z_NAME
        else:
            # Закончили с товарами — идём к курсам
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

# ======== /FF ========

F_SELECT_ORDER, F_MAIN_MENU, F_SINGLE_ITEMS, F_BUNDLE_CREATE, F_BUNDLE_DIMS, F_BUNDLE_PACKAGE, F_PACKAGES, F_BUNDLE_THERMAL, F_BUNDLE_WORK, F_SUMMARY = range(10)

async def cmd_ff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    
    # Если передано имя клиента — ищем в базе как /zakaz
    if context.args:
        client_name = ' '.join(context.args)
        
        # Получаем заказы клиента
        client_orders, error = await get_client_orders_from_notion(client_name)
        
        if client_orders is None:
            # Ошибка при получении
            logger.error(f"Ошибка получения заказов: {error}")
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
            'all_client_orders': client_orders
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
            # Новый клиент
            await update.message.reply_text(
                f'Клиент: <b>{client_name}</b>\n'
                f'В базе заказов не найдено.\n\n'
                f'Для нового заказа сначала выполни /zakaz {client_name}'
            )
            return ConversationHandler.END
    
    # Без аргументов — работаем как раньше (нужен предыдущий /zakaz)
    if uid not in orders or not orders[uid].get('items'):
        await update.message.reply_text(
            'Сначала выполни /zakaz [имя клиента]\n\n'
            'Или сразу: /ff [имя клиента]'
        )
        return ConversationHandler.END
    
    result = await show_order_selection(update, context, uid, F_SELECT_ORDER)
    if result is None:
        return ConversationHandler.END
    if result == 'single':
        await start_ff(update, context, uid)
        return F_PACKAGES
    return F_SELECT_ORDER

async def show_order_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, uid, next_step):
    if uid not in orders:
        await update.message.reply_text('Сначала выполни /zakaz [имя клиента]')
        return None
    
    client_orders = orders[uid].get('all_client_orders', [])
    client = orders[uid].get('client', 'Неизвестно')
    
    if not client_orders:
        # Нет заказов в базе — работаем с текущим
        if orders[uid].get('items'):
            return 'single'
        await update.message.reply_text('Нет данных для расчёта. Сначала выполни /zakaz')
        return None
    
    if len(client_orders) == 1:
        await load_order_data(uid, 0)
        return 'single'
    
    keyboard = []
    for idx, order in enumerate(client_orders[:5]):
        # Формируем понятное описание товаров
        items_list = order.get('items', [])
        if items_list:
            # Показываем только названия (без количества, т.к. в Notion его нет)
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
        f'Клиент: <b>{client}</b>\n\nВыбери заказ:',
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )
    return next_step

async def load_order_data(uid, order_idx):
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
        # Сохраняем ID страницы Notion для обновления
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

async def f_select_order_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = str(update.effective_user.id)
    
    if query.data == 'sel_order_new':
        await query.edit_message_text('Для нового заказа сначала выполни /zakaz [имя]')
        return ConversationHandler.END
    
    try:
        order_idx = int(query.data.replace('sel_order_', ''))
        await load_order_data(uid, order_idx)
        
        order = orders[uid]['all_client_orders'][order_idx]
        items_list = order.get('items', [])
        if items_list:
            items_summary = ", ".join([i['name'] for i in items_list[:3] if i.get('name')])
            if len(items_list) > 3:
                items_summary += f" +{len(items_list)-3}"
        else:
            items_summary = order.get('items_text', 'Товар')[:40]
        
        # Проверяем, есть ли товары с размерами
        if not orders[uid].get('items') or not any(i.get('dims', (0,0,0))[0] > 0 for i in orders[uid]['items']):
            await query.edit_message_text(
                f'⚠️ Выбрано: {items_summary}\n\n'
                f'В заказе нет размеров товаров.\n'
                f'Нужно пересчитать через /zakaz {orders[uid]["client"]}'
            )
            return ConversationHandler.END
        
        await query.edit_message_text(f'📦 FF: {items_summary}')
        await start_ff(update, context, uid)
        return F_PACKAGES
    except Exception as e:
        logger.error(f"Ошибка в f_select_order_cb: {e}")
        await query.edit_message_text(f'❌ Ошибка: {str(e)[:100]}')
        return ConversationHandler.END

async def start_ff(update: Update, context: ContextTypes.DEFAULT_TYPE, uid):
    # Для наборов — 1 коробка на набор (не умножаем на qty)
    # Для обычных товаров — считаем как раньше
    boxes = 0
    for item in orders[uid]['items']:
        if item.get('is_bundle'):
            # Набор — считаем как 1 коробка на набор
            boxes += 1
        else:
            # Обычный товар — используем сохраненное или считаем
            boxes += item.get('boxes', 1)
    
    orders[uid]['ff_boxes_total'] = FF_BOX_PRICE * boxes
    orders[uid]['ff_boxes_count'] = boxes
    
    orders[uid]['ff_packages'] = {}
    orders[uid]['ff_index'] = 0
    
    await show_ff_package(update, context, uid)

async def show_ff_package(update_or_query, context, uid):
    idx = orders[uid]['ff_index']
    items = orders[uid]['items']
    
    # Получаем chat_id для отправки сообщений
    chat_id = None
    if hasattr(update_or_query, 'callback_query') and update_or_query.callback_query:
        chat_id = update_or_query.callback_query.message.chat_id
    elif hasattr(update_or_query, 'message') and update_or_query.message:
        chat_id = update_or_query.message.chat_id
    elif hasattr(update_or_query, 'chat_id'):
        chat_id = update_or_query.chat_id
    
    if idx >= len(items):
        boxes = orders[uid]['ff_boxes_count']
        box_total = orders[uid]['ff_boxes_total']
        msg = f"📦 FF Коробки: {FF_BOX_PRICE} ¥ × {boxes} = {fmt(box_total)} ¥ (авто)\n\nFF — Работа (¥ за 1 шт):"
        if chat_id:
            await context.bot.send_message(chat_id=chat_id, text=msg)
        else:
            await update_or_query.message.reply_text(msg)
        return F_WORK
    
    item = items[idx]
    l, w, h = item['dims']
    qty = item['qty']
    is_bundle = item.get('is_bundle', False)
    
    # Получаем все пакеты из Notion для ручного выбора
    packages = await get_packages_from_notion()
    orders[uid]['ff_available_packages'] = packages  # Сохраняем для callback
    
    bundle_label = "📦 НАБОР" if is_bundle else "📦 Товар"
    qty_text = f"Кол-во наборов: {qty}" if is_bundle else f"Кол-во: {qty} шт"
    
    msg = f"{bundle_label} {idx+1}/{len(items)}: <b>{item['name']}</b>\n"
    msg += f"📐 Размеры: {int(l)}×{int(w)}×{int(h)} см | {qty_text}\n\n"
    
    if is_bundle:
        msg += f"<b>Для НАБОРА:</b> 1 пакет на весь набор\n"
        msg += f"Выбери пакет из базы:\n\n"
    else:
        msg += f"<b>Выбери пакет из базы:</b>"
    
    # Создаём кнопки для всех пакетов
    keyboard = []
    for pkg_idx, pkg in enumerate(packages):
        btn_text = f"📦 {pkg['name']} — {pkg['price']}¥ ({int(pkg['l'])}×{int(pkg['w'])}×{int(pkg['h'])}см)"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f'f_pkg_select_{pkg_idx}')])
    
    # Кнопка для своей цены
    keyboard.append([InlineKeyboardButton("💰 Своя цена", callback_data='f_pkg_custom')])
    
    if hasattr(update_or_query, 'edit_message_text'):
        await update_or_query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    else:
        await update_or_query.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

async def f_package_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = str(update.effective_user.id)
    data = query.data
    
    if data.startswith('f_pkg_select_'):
        # Пользователь выбрал конкретный пакет из списка
        pkg_idx = int(data.replace('f_pkg_select_', ''))
        packages = orders[uid].get('ff_available_packages', [])
        
        if pkg_idx < len(packages):
            selected_pkg = packages[pkg_idx]
            idx = orders[uid]['ff_index']
            items = orders[uid]['items']
            item = items[idx]
            qty = item['qty']
            is_bundle = item.get('is_bundle', False)
            
            # Для набора — 1 пакет на весь набор
            # Для обычного товара — пакет на каждую штуку
            if is_bundle:
                pkg_total = selected_pkg['price']  # 1 пакет на набор
                qty_display = 1
            else:
                pkg_total = selected_pkg['price'] * qty
                qty_display = qty
            
            orders[uid]['ff_packages'][idx] = {
                'pkg': selected_pkg, 
                'total': pkg_total, 
                'qty': qty_display,
                'is_bundle': is_bundle
            }
            
            # Подтверждаем выбор
            if is_bundle:
                await query.edit_message_text(
                    f"✅ Выбран для НАБОРА: <b>{selected_pkg['name']}</b>\n"
                    f"   1 пакет × {selected_pkg['price']}¥ = {fmt(pkg_total)}¥\n"
                    f"   (на {qty} наборов)"
                )
            else:
                await query.edit_message_text(
                    f"✅ Выбран: <b>{selected_pkg['name']}</b>\n"
                    f"   {selected_pkg['price']}¥ × {qty} = {fmt(pkg_total)}¥"
                )
            
            orders[uid]['ff_index'] += 1
            result = await show_ff_package(update, context, uid)
            if result == F_WORK:
                return F_WORK
            return F_PACKAGES
        else:
            await query.edit_message_text('❌ Ошибка: пакет не найден')
            return F_PACKAGES
    elif data == 'f_pkg_custom':
        await query.edit_message_text('Введи цену пакетов (¥):')
        return F_PACKAGE_PRICE

async def f_package_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try:
        price = float(update.message.text)
        idx = orders[uid]['ff_index']
        items = orders[uid]['items']
        item = items[idx]
        qty = item['qty']
        is_bundle = item.get('is_bundle', False)
        
        # Для набора — 1 пакет на весь набор
        if is_bundle:
            total_price = price
            qty_display = 1
        else:
            total_price = price * qty
            qty_display = qty
        
        orders[uid]['ff_packages'][idx] = {
            'pkg': {'name': 'Пакет (ручной)'},
            'total': total_price,
            'qty': qty_display,
            'is_bundle': is_bundle
        }
        orders[uid]['ff_index'] += 1
        
        if orders[uid]['ff_index'] >= len(items):
            boxes = orders[uid]['ff_boxes_count']
            box_total = orders[uid]['ff_boxes_total']
            await update.message.reply_text(
                f'📦 FF Коробки: {FF_BOX_PRICE} ¥ × {boxes} = {fmt(box_total)} ¥ (авто)\n\n'
                f'FF — Работа (¥ за 1 шт):'
            )
            return F_WORK
        else:
            await show_ff_package(update, context, uid)
            return F_PACKAGES
    except:
        await update.message.reply_text('Число! Введи цену:')
        return F_PACKAGE_PRICE

async def f_work(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try:
        price = float(update.message.text)
        total_qty = sum(i['qty'] for i in orders[uid]['items'])
        orders[uid]['ff_work'] = price * total_qty
        
        # Считаем сколько товаров и пакетов
        items_count = len(orders[uid]['items'])
        bundles_count = sum(1 for i in orders[uid]['items'] if i.get('is_bundle'))
        regular_count = items_count - bundles_count
        
        msg = "📝 FF — Термобумага (0.016¥)\n\n"
        msg += f"У тебя:\n"
        if regular_count > 0:
            msg += f"• {regular_count} обычных товаров\n"
        if bundles_count > 0:
            msg += f"• {bundles_count} наборов\n"
        msg += f"\nВведи общее количество листов термобумаги:\n"
        msg += f"(на товары + на пакеты)"
        
        await update.message.reply_text(msg)
        return F_THERMAL
    except:
        await update.message.reply_text('Число!')
        return F_WORK

async def f_thermal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try:
        total_sheets = float(update.message.text)
        # Общее количество листов умножаем на цену
        orders[uid]['ff_thermal'] = 0.016 * total_sheets
        
        packages_total = sum(p['total'] for p in orders[uid].get('ff_packages', {}).values())
        boxes = orders[uid].get('ff_boxes_total', 0)
        work = orders[uid]['ff_work']
        thermal = orders[uid]['ff_thermal']
        
        ff_total = packages_total + boxes + work + thermal
        orders[uid]['ff_total_yuan'] = ff_total
        
        real_rate = orders[uid].get('real_rate', 55)
        ff_amd = int(ff_total * real_rate)
        
        # Показываем детализацию по наборам
        msg = f"📦 <b>FF Китай</b>\n\n"
        
        for idx, pkg_data in orders[uid].get('ff_packages', {}).items():
            item = orders[uid]['items'][idx]
            if item.get('is_bundle'):
                msg += f"📦 <b>{item['name']}</b> (НАБОР)\n"
                msg += f"   1 пакет × {pkg_data['pkg']['price']}¥ = {fmt(pkg_data['total'])}¥\n"
            else:
                msg += f"📦 {item['name']}\n"
                msg += f"   {pkg_data['qty']} пакетов × {pkg_data['pkg']['price']}¥ = {fmt(pkg_data['total'])}¥\n"
        
        msg += f"\nКоробки: {fmt(boxes)}¥\n"
        msg += f"Работа: {fmt(work)}¥\n"
        msg += f"Термобумага ({int(total_sheets)} листов): {fmt(thermal)}¥\n"
        msg += f"━━━━━━━━━━━━\n"
        msg += f"<b>Итого FF: {fmt(ff_total)}¥ = {ff_amd} AMD</b>\n\n"
        msg += f"Для доставки РФ используй <b>/dostavka</b>"
        
        await update.message.reply_text(msg, parse_mode='HTML')
        
        await update.message.reply_text(msg, parse_mode='HTML')
        
        # === АВТО-СОХРАНЕНИЕ В NOTION ===
        has_access, _ = await check_notion_access()
        if has_access:
            try:
                notion_url = await save_to_notion(update, context, uid)
                if notion_url:
                    await update.message.reply_text(f"✅ Сохранено в Notion:\n{notion_url}", parse_mode='HTML')
            except Exception as e:
                logger.error(f"Ошибка сохранения FF в Notion: {e}")
        
        save_session()
        return ConversationHandler.END
    except:
        await update.message.reply_text('Число!')
        return F_THERMAL

# ======== /DOSTAVKA ========

D_SELECT_ORDER, D_WAREHOUSE, D_BOXES, D_MORE_WH, D_RUB_RATE, D_CRATING = range(6)

async def cmd_dostavka(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    
    # Если передано имя клиента — ищем в базе как /zakaz
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
            'type': 'dostavka',
            'all_client_orders': client_orders
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
                f'🚚 FILLX Доставка РФ\nКлиент: <b>{client_name}</b>\n'
                f'Найдено заказов: {len(client_orders)}\n\n'
                f'Выбери заказ:',
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='HTML'
            )
            return D_SELECT_ORDER
        else:
            await update.message.reply_text(
                f'Клиент: <b>{client_name}</b>\n'
                f'В базе заказов не найдено.\n\n'
                f'Для нового заказа сначала выполни /zakaz {client_name}'
            )
            return ConversationHandler.END
    
    # Без аргументов — работаем как раньше
    if uid not in orders or not orders[uid].get('items'):
        await update.message.reply_text(
            'Сначала выполни /zakaz [имя клиента]\n\n'
            'Или сразу: /dostavka [имя клиента]'
        )
        return ConversationHandler.END
    
    result = await show_order_selection(update, context, uid, D_SELECT_ORDER)
    if result is None:
        return ConversationHandler.END
    if result == 'single':
        await start_dostavka(update, context, uid)
        return D_WAREHOUSE
    return D_SELECT_ORDER

async def start_dostavka(update: Update, context: ContextTypes.DEFAULT_TYPE, uid):
    orders[uid]['warehouses'] = []
    
    keyboard = []
    cities = [c for c in TARIFFS.keys() if '(' not in c]
    for i in range(0, len(cities), 2):
        row = [InlineKeyboardButton(cities[i], callback_data=f'd_wh_{cities[i]}')]
        if i + 1 < len(cities):
            row.append(InlineKeyboardButton(cities[i+1], callback_data=f'd_wh_{cities[i+1]}'))
        keyboard.append(row)
    
    await update.message.reply_text('Выбери склад РФ:', reply_markup=InlineKeyboardMarkup(keyboard))

async def d_select_order_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = str(update.effective_user.id)
    
    if query.data == 'sel_order_new':
        await query.edit_message_text('Для нового заказа сначала выполни /zakaz [имя]')
        return ConversationHandler.END
    
    try:
        order_idx = int(query.data.replace('sel_order_', ''))
        await load_order_data(uid, order_idx)
        
        order = orders[uid]['all_client_orders'][order_idx]
        items_list = order.get('items', [])
        if items_list:
            items_summary = ", ".join([i['name'] for i in items_list[:3] if i.get('name')])
            if len(items_list) > 3:
                items_summary += f" +{len(items_list)-3}"
        else:
            items_summary = order.get('items_text', 'Товар')[:40]
        
        await query.edit_message_text(f'Выбрано: {items_summary}')
        await start_dostavka(update, context, uid)
        return D_WAREHOUSE
    except Exception as e:
        logger.error(f"Ошибка в d_select_order_cb: {e}")
        await query.edit_message_text(f'❌ Ошибка: {str(e)[:100]}')
        return ConversationHandler.END

async def d_warehouse_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = str(update.effective_user.id)
    
    city = query.data.replace('d_wh_', '')
    orders[uid]['current_wh'] = city
    
    await query.edit_message_text(f'📦 {city}\nСколько коробок?')
    return D_BOXES

async def d_boxes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try:
        boxes = int(update.message.text)
        city = orders[uid]['current_wh']
        
        if 'Свой тариф' in city:
            await update.message.reply_text('Введи тариф (₽ за коробку):')
            return D_BOXES
        else:
            tariff = TARIFFS[city]
        
        cost = tariff * boxes
        orders[uid]['warehouses'].append({'city': city, 'boxes': boxes, 'tariff': tariff, 'cost': cost})
        
        keyboard = [
            [InlineKeyboardButton("Да", callback_data='d_more_yes')],
            [InlineKeyboardButton("Нет", callback_data='d_more_no')]
        ]
        await update.message.reply_text(
            f'📦 {city}: {tariff}₽ × {boxes} = {cost}₽\n\nДобавить ещё склад?',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return D_MORE_WH
    except:
        await update.message.reply_text('Число!')
        return D_BOXES

async def d_more_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = str(update.effective_user.id)
    
    if query.data == 'd_more_yes':
        keyboard = []
        cities = [c for c in TARIFFS.keys() if '(' not in c]
        for i in range(0, len(cities), 2):
            row = [InlineKeyboardButton(cities[i], callback_data=f'd_wh_{cities[i]}')]
            if i + 1 < len(cities):
                row.append(InlineKeyboardButton(cities[i+1], callback_data=f'd_wh_{cities[i+1]}'))
            keyboard.append(row)
        await query.edit_message_text('Выбери склад РФ:', reply_markup=InlineKeyboardMarkup(keyboard))
        return D_WAREHOUSE
    else:
        if 'rub_rate' in orders[uid]:
            await query.edit_message_text(f'Курс ₽→драм ({orders[uid]["rub_rate"]}):\n(отправь новое число или "ok")')
        else:
            await query.edit_message_text('Курс ₽→драм (например 5.8):')
        return D_RUB_RATE

async def d_rub_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    text = update.message.text.strip().lower()
    
    try:
        if text and text != 'ok':
            orders[uid]['rub_rate'] = float(text)
        
        keyboard = [[InlineKeyboardButton("Да", callback_data='d_crate_yes'), 
                     InlineKeyboardButton("Нет", callback_data='d_crate_no')]]
        await update.message.reply_text('FILLX — Снятие обрешётки (2000₽)?', reply_markup=InlineKeyboardMarkup(keyboard))
        return D_CRATING
    except:
        await update.message.reply_text('Число или "ok"! Курс ₽→драм:')
        return D_RUB_RATE

async def d_crating_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = str(update.effective_user.id)
    
    try:
        crating = 2000 if query.data == 'd_crate_yes' else 0
        orders[uid]['crating'] = crating
        
        rub_rate = orders[uid].get('rub_rate', 5.8)
        total_boxes = sum(w['boxes'] for w in orders[uid]['warehouses'])
        
        fillx_pickup = 7000
        fillx_receiving = 1000 * total_boxes
        fillx_unpacking = 500 * total_boxes
        fillx_delivery = sum(w['cost'] for w in orders[uid]['warehouses'])
        
        fillx_total = fillx_pickup + crating + fillx_receiving + fillx_delivery + fillx_unpacking
        fillx_amd = int(fillx_total * rub_rate)
        
        wh_text = "\n".join([f"📦 {w['city']}: {w['tariff']}₽ × {w['boxes']} = {w['cost']}₽" 
                            for w in orders[uid]['warehouses']])
        
        msg = f"📦 <b>FILLX Доставка РФ</b>\n"
        msg += f"Курс: {rub_rate} ₽→драм\n\n"
        msg += f"{wh_text}\n\n"
        msg += f"Забор IOB: 7000₽\n"
        msg += f"Обрешётка: {crating}₽\n"
        msg += f"Приёмка: {fillx_receiving}₽\n"
        msg += f"Доставка: {fillx_delivery}₽\n"
        msg += f"Разбор: {fillx_unpacking}₽\n"
        msg += f"━━━━━━━━━━━━\n"
        msg += f"<b>Итого FILLX: {fillx_total}₽ = {fillx_amd} AMD</b>"
        
        orders[uid]['fillx_total'] = fillx_total
        orders[uid]['fillx_amd'] = fillx_amd
        
        await query.edit_message_text(msg, parse_mode='HTML')
        
        # Сохраняем только если Notion доступен
        has_access, _ = await check_notion_access()
        if has_access:
            try:
                notion_url = await save_to_notion(update, context, uid)
                if notion_url:
                    await context.bot.send_message(
                        chat_id=update.callback_query.message.chat_id,
                        text=f"✅ Сохранено в Notion:\n{notion_url}",
                        parse_mode='HTML'
                    )
            except Exception as e:
                logger.error(f"Ошибка сохранения FILLX в Notion: {e}")
                await context.bot.send_message(
                    chat_id=update.callback_query.message.chat_id,
                    text=f"⚠️ Ошибка сохранения: {str(e)[:100]}",
                    parse_mode='HTML'
                )
        else:
            await context.bot.send_message(
                chat_id=update.callback_query.message.chat_id,
                text="⚠️ Заказ рассчитан, но не сохранён в Notion (нет доступа)"
            )
        
        save_session()
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Ошибка в d_crating_cb: {e}")
        await query.edit_message_text(f'❌ Ошибка: {str(e)[:100]}')
        return ConversationHandler.END

async def save_to_notion(update, context, uid):
    try:
        existing_fields = await get_notion_fields()
        existing_names = [f.split(' (')[0] for f in existing_fields]
        
        data = orders[uid]
        client = data['client']
        items = data['items']
        
        total_qty = sum(i['qty'] for i in items)
        total_purchase = sum(i['purchase'] * i['qty'] for i in items)
        total_price = sum(i['price'] * i['qty'] for i in items)
        delivery_factory = sum(i.get('delivery_factory', 0) for i in items)
        
        order_code = get_code(client)
        
        ff_total = data.get('ff_total_yuan', 0)
        client_rate = data.get('client_rate', 58)
        real_rate = data.get('real_rate', 55)
        rub_rate = data.get('rub_rate', 5.8)
        
        # Новые поля с правильным расчётом
        commission_cny = data.get('commission_cny', 0)
        commission_amd = data.get('commission_amd', 0)
        total_cny = data.get('total_cny', 0)
        total_with_commission_cny = data.get('total_with_commission_cny', 0)
        total_amd = data.get('total_amd', 0)
        
        fillx_total = data.get('fillx_total', 0)
        fillx_amd = data.get('fillx_amd', 0)
        
        ff_amd = int(ff_total * real_rate) if ff_total else 0
        purchase_amd = int((total_purchase + delivery_factory) * real_rate)
        
        # Общие расходы (закупка + FF + FILLX)
        total_costs = purchase_amd + ff_amd + fillx_amd
        
        # Прибыль = что получили от клиента - расходы
        profit = total_amd - total_costs
        
        properties = {}
        
        field_mapping = {
            "Код заказа": ("title", order_code),
            "Клиент": ("select", client),
            "Описание товара": ("rich_text", '; '.join([i['name'] for i in items])),
            "Количество": ("number", int(total_qty)),
            "Цена клиенту (CNY)": ("number", float(total_price)),
            "Цена закупки (CNY)": ("number", float(total_purchase)),
            "Доставка (CNY)": ("number", float(delivery_factory)),
            "Курс клиенту": ("number", float(client_rate)),
            "Курс реальный": ("number", float(real_rate)),
            "Курс ₽→драм": ("number", float(rub_rate)),
            "Закупка реальная (AMD)": ("number", purchase_amd),
            "Прибыль (AMD)": ("number", profit),
            "К ОПЛАТЕ (AMD)": ("number", total_amd),
            "FF Итого (CNY)": ("number", ff_total),
            "FF Итого (AMD)": ("number", ff_amd),
            "FILLX Итого (₽)": ("number", fillx_total),
            "FILLX Итого (AMD)": ("number", fillx_amd),
            "Комиссия (CNY)": ("number", commission_cny),
            "Комиссия (AMD)": ("number", commission_amd),
            "Статус": ("select", "Новый"),
        }
        
        for field_name, (field_type, value) in field_mapping.items():
            if field_name in existing_names:
                if field_type == "title":
                    properties[field_name] = {"title": [{"text": {"content": str(value)}}]}
                elif field_type == "rich_text":
                    properties[field_name] = {"rich_text": [{"text": {"content": str(value)}}]}
                elif field_type == "select":
                    properties[field_name] = {"select": {"name": str(value)}}
                elif field_type == "number":
                    properties[field_name] = {"number": float(value) if value else 0}
        
        if properties:
            # Проверяем, есть ли ID страницы для обновления
            existing_page_id = orders[uid].get('notion_page_id')
            
            if existing_page_id:
                # Обновляем существующую запись
                result = notion.pages.update(page_id=existing_page_id, properties=properties)
                page_id = existing_page_id
                action = "Обновлено"
            else:
                # Создаём новую запись
                result = notion.pages.create(parent={"database_id": NOTION_DATABASE_ID}, properties=properties)
                page_id = result.get('id', '')
                orders[uid]['notion_page_id'] = page_id  # Сохраняем ID для следующих обновлений
                action = "Сохранено"
            
            # Собираем URL вручную, так как API может не возвращать его
            page_url = f"https://notion.so/{page_id.replace('-', '')}"
            logger.info(f"✅ {action} в Notion: {order_code} - {page_url}")
            return page_url
        else:
            logger.warning("⚠️ Нет подходящих полей в Notion для сохранения")
            return None
    except Exception as e:
        logger.error(f"Notion error: {e}")
        logger.error(traceback.format_exc())
        return None

# ======== MAIN ========

def main():
    load_session()
    
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # /zakaz
    zakaz_conv = ConversationHandler(
        entry_points=[CommandHandler('zakaz', cmd_zakaz)],
        states={
            Z_INVOICE: [CallbackQueryHandler(z_invoice_cb, pattern='^z_invoice_')],
            Z_SELECT_ORDER: [CallbackQueryHandler(z_select_order_cb, pattern='^z_sel_')],
            Z_ORDER_ACTION: [CallbackQueryHandler(z_order_action_cb, pattern='^z_act_')],
            Z_SELECT_ITEMS: [CallbackQueryHandler(z_item_toggle_cb, pattern='^z_item_toggle_|^z_items_done$|^z_items_back$')],
            Z_EDIT_ITEM_QTY: [MessageHandler(filters.TEXT & ~filters.COMMAND, z_edit_item_qty)],
            Z_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, z_get_name)],
            Z_QTY: [MessageHandler(filters.TEXT & ~filters.COMMAND, z_get_qty)],
            Z_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, z_get_price)],
            Z_PURCHASE: [MessageHandler(filters.TEXT & ~filters.COMMAND, z_get_purchase)],
            Z_DELIVERY: [MessageHandler(filters.TEXT & ~filters.COMMAND, z_get_delivery)],
            Z_BUNDLE_SELECT: [CallbackQueryHandler(z_bundle_select_cb, pattern='^z_bundle_')],
            Z_BUNDLE_NEW: [MessageHandler(filters.TEXT & ~filters.COMMAND, z_bundle_new_name)],
            Z_DIMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, z_get_dims)],
            Z_MORE: [CallbackQueryHandler(z_more_cb, pattern='^z_more_')],
            Z_CLIENT_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, z_client_rate)],
            Z_REAL_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, z_real_rate)],
            Z_COMMISSION: [CallbackQueryHandler(z_commission_cb, pattern='^z_comm_')],
        },
        fallbacks=[CommandHandler('cancel', cmd_cancel)],
    )
    app.add_handler(zakaz_conv)
    
    # /ff
    ff_conv = ConversationHandler(
        entry_points=[CommandHandler('ff', cmd_ff)],
        states={
            F_SELECT_ORDER: [CallbackQueryHandler(f_select_order_cb, pattern='^sel_order_')],
            F_PACKAGES: [CallbackQueryHandler(f_package_cb, pattern='^f_pkg_select_|^f_pkg_custom$')],
            F_PACKAGE_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, f_package_price)],
            F_WORK: [MessageHandler(filters.TEXT & ~filters.COMMAND, f_work)],
            F_THERMAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, f_thermal)],
        },
        fallbacks=[CommandHandler('cancel', cmd_cancel)],
    )
    app.add_handler(ff_conv)
    
    # /dostavka
    dostavka_conv = ConversationHandler(
        entry_points=[CommandHandler('dostavka', cmd_dostavka)],
        states={
            D_SELECT_ORDER: [CallbackQueryHandler(d_select_order_cb, pattern='^sel_order_')],
            D_WAREHOUSE: [CallbackQueryHandler(d_warehouse_cb, pattern='^d_wh_')],
            D_BOXES: [MessageHandler(filters.TEXT & ~filters.COMMAND, d_boxes)],
            D_MORE_WH: [CallbackQueryHandler(d_more_cb, pattern='^d_more_')],
            D_RUB_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, d_rub_rate)],
            D_CRATING: [CallbackQueryHandler(d_crating_cb, pattern='^d_crate_')],
        },
        fallbacks=[CommandHandler('cancel', cmd_cancel)],
    )
    app.add_handler(dostavka_conv)
    
    logger.info("Bot starting...")
    app.run_polling()

if __name__ == '__main__':
    main()