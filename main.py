import os
import logging
import math
import json
import traceback
import re
from datetime import datetime
from notion_client import Client
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ConversationHandler, filters, ContextTypes

# ======== НАСТРОЙКИ И ЛОГИРОВАНИЕ ========
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
NOTION_TOKEN = os.getenv('NOTION_TOKEN')
NOTION_DATABASE_ID = "3278c4d1fb0e80c4b6e5f261d0631ed2"
PACKAGES_DATABASE_ID = "32a8c4d1fb0e806ebb98f5995704d0e5"

notion = Client(auth=NOTION_TOKEN) if NOTION_TOKEN else None

orders = {}
TARIFFS = {
    'Коледино': 350, 'Невинномысск': 1100, 'Электросталь': 400, 'Белые Столбы': 350,
    'Чашниково': 350, 'Санкт-Петербург': 450, 'Казань': 450, 'Екатеринбург': 700,
    'Новосибирск': 850, 'Владивосток': 1000, 'Краснодар': 550, 'Свой тариф': 0
}

# ======== УТИЛИТЫ ========
def save_session():
    try:
        with open('orders_session.json', 'w', encoding='utf-8') as f:
            json.dump(orders, f, ensure_ascii=False, indent=2)
    except Exception as e: logger.error(f"Save error: {e}")

def load_session():
    global orders
    try:
        with open('orders_session.json', 'r', encoding='utf-8') as f:
            orders = json.load(f)
    except: orders = {}

def get_code(client):
    return f"{client.upper().replace(' ', '-')}-{datetime.now().strftime('%y%m%d')}"

def normalize_client_name(name):
    """Удаляет пробелы и делает первую букву заглавной (Zaven 8291 -> Zaven8291)"""
    return re.sub(r'\s+', '', name).capitalize()

def optimize_boxes(items):
    """ПРАВИЛЬНЫЙ АЛГОРИТМ 3D: Считает сколько коробок 60x40x40 понадобится"""
    MAX_L, MAX_W, MAX_H = 60, 40, 40
    boxes = 0
    remaining_vol = 0
    
    for item in items:
        l, w, h = item.get('dims', (0,0,0))
        qty = item.get('qty', 0)
        if l*w*h == 0 or qty <= 0: continue
        
        best_fit = 0
        for rot_l, rot_w, rot_h in [(l,w,h), (l,h,w), (w,l,h), (w,h,l), (h,l,w), (h,w,l)]:
            fit = int(MAX_L // rot_l) * int(MAX_W // rot_w) * int(MAX_H // rot_h)
            if fit > best_fit: best_fit = fit
        
        if best_fit == 0: 
            boxes += qty 
            continue
            
        item_vol = l * w * h
        
        if remaining_vol >= item_vol:
            fit_in_rem = min(qty, int(remaining_vol // item_vol))
            qty -= fit_in_rem
            remaining_vol -= fit_in_rem * item_vol
            
        if qty > 0:
            needed_boxes = qty // best_fit
            boxes += needed_boxes
            leftover = qty % best_fit
            
            if leftover > 0:
                boxes += 1
                remaining_vol = (MAX_L * MAX_W * MAX_H) - (leftover * item_vol)
                
    return int(boxes)

# ======== NOTION API ========
async def get_packages_from_notion():
    if not notion or not PACKAGES_DATABASE_ID: return []
    try:
        res = notion.databases.query(database_id=PACKAGES_DATABASE_ID)
        packages = []
        for page in res.get('results', []):
            props = page['properties']
            title_prop = props.get('Название', {}).get('title', [])
            name = title_prop[0].get('text', {}).get('content', '') if title_prop else ''
            price = props.get('Цена', {}).get('number') or 0
            if name and price > 0:
                packages.append({'name': name, 'price': price})
        return packages
    except Exception as e:
        logger.error(f"Error reading packages: {e}")
        return []

async def get_client_orders_from_notion(client_name):
    if not notion or not NOTION_DATABASE_ID: return None, "Notion не настроен"
    try:
        res = notion.databases.query(database_id=NOTION_DATABASE_ID, sorts=[{"timestamp": "created_time", "direction": "descending"}], page_size=100)
        client_norm = normalize_client_name(client_name).lower()
        filtered = []
        
        for p in res.get('results', []):
            tag = p['properties'].get('Клиент', {}).get('select', {})
            if tag:
                tag_name_norm = normalize_client_name(tag.get('name', '')).lower()
                if tag_name_norm == client_norm:
                    filtered.append(p)
        
        orders_list = []
        for page in filtered[:10]:
            props = page['properties']
            desc_text = props.get('Описание товара', {}).get('rich_text', [{}])[0].get('text', {}).get('content', '') if props.get('Описание товара', {}).get('rich_text') else ''
            items_list = [{'name': item_str.strip(), 'qty': 0} for item_str in desc_text.split(';') if item_str.strip()]
            orders_list.append({
                'id': page['id'],
                'code': props.get('Код заказа', {}).get('title', [{}])[0].get('text', {}).get('content', '') if props.get('Код заказа', {}).get('title') else '',
                'date': page.get('created_time', '')[:10],
                'client_rate': props.get('Курс клиенту', {}).get('number'),
                'real_rate': props.get('Курс реальный', {}).get('number'),
                'items': items_list
            })
        return orders_list, None
    except Exception as e: return None, str(e)

async def save_to_notion(uid):
    if not notion: return None
    try:
        data = orders[uid]
        client = data.get('client', 'Unknown')
        items = data.get('items', [])
        
        total_qty = sum(i.get('qty', 0) for i in items)
        total_purchase_cny = sum(i.get('purchase', 0) * i.get('qty', 0) for i in items)
        total_price_client_cny = sum(i.get('price', 0) * i.get('qty', 0) for i in items)
        delivery_cny = sum(i.get('delivery_factory', 0) for i in items)
        
        real_rate = data.get('real_rate', 55)
        client_rate = data.get('client_rate', 58)
        total_amd = data.get('final_total_amd', data.get('total_amd', 0))
        purchase_real_amd = int((total_purchase_cny + delivery_cny) * real_rate)
        desc_text = '; '.join([f"{i['name']} x {i.get('qty', 0)}" for i in items])
        
        properties = {
            "Код заказа": {"title": [{"text": {"content": get_code(client)}}]},
            "Клиент": {"select": {"name": client}}, # Здесь сохраняется нормализованное имя
            "Описание товара": {"rich_text": [{"text": {"content": desc_text}}]},
            "Количество": {"number": float(total_qty)},
            "Цена клиенту (CNY)": {"number": float(total_price_client_cny)},
            "Цена закупки (CNY)": {"number": float(total_purchase_cny)},
            "Доставка (CNY)": {"number": float(delivery_cny)},
            "Курс клиенту": {"number": float(client_rate)},
            "Курс реальный": {"number": float(real_rate)},
            "Закупка реальная (AMD)": {"number": float(purchase_real_amd)},
            "На закупку (AMD)": {"number": float(purchase_real_amd)},
            "На закупку (CNY)": {"number": float(total_purchase_cny + delivery_cny)},
            " К ОПЛАТЕ (AMD)": {"number": float(total_amd)},
            " Инвойс": {"select": {"name": "Да" if data.get('invoice_needed') else "Нет"}},
            "Статус": {"select": {"name": "Новый"}},
            "Date": {"date": {"start": datetime.now().strftime('%Y-%m-%d')}}
        }

        if 'ff_total_yuan' in data: properties["ИТОГО (CNY)"] = {"number": float(data['ff_total_yuan'])}
        if 'rub_rate' in data: properties["Курс ₽→драм"] = {"number": float(data['rub_rate'])}

        page_id = data.get('notion_page_id')
        if page_id: 
            res = notion.pages.update(page_id=page_id, properties=properties)
        else:
            res = notion.pages.create(parent={"database_id": NOTION_DATABASE_ID}, properties=properties)
            orders[uid]['notion_page_id'] = res['id']
            
        return f"https://notion.so/{res['id'].replace('-', '')}"
    except Exception as e: logger.error(f"Notion Error: {e}"); return None

# ======== КОМАНДЫ БОТА ========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 <b>GS Orders Bot</b>\n/zakaz, /ff, /dostavka, /paste", parse_mode='HTML')

# --- /PASTE КОМАНДА ---
async def cmd_paste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    text = update.message.text.replace('/paste', '').strip()
    
    if not text:
        await update.message.reply_text('Вставь расчёт. Пример:\nКлиент: Zaven8291\nТовар 1:\nНазвание: Крепление\nКоличество: 400\nЦена клиенту: 6.2\nЗакупка: 4.5\nДоставка: 43.6\nРазмеры: 16 12 5\nКурс клиенту: 58\nМой курс: 55')
        return
        
    lines = text.strip().split('\n')
    client = 'Unknown'; items = []; client_rate = 58; real_rate = 55; current_item = None
    
    for line in lines:
        line = line.strip().lower()
        if not line: continue
        if line.startswith('клиент:'): 
            raw_client = line.split(':', 1)[1].strip()
            client = normalize_client_name(raw_client)
        elif line.startswith('товар'):
            if current_item and current_item.get('name'): items.append(current_item)
            current_item = {'dims': (0,0,0)}
        elif line.startswith('название:'): 
            if current_item is None: current_item = {'dims': (0,0,0)}
            current_item['name'] = line.split(':', 1)[1].strip().title()
        elif line.startswith('количество:'): current_item['qty'] = int(line.split(':', 1)[1].strip() or 0)
        elif line.startswith('цена клиенту:'): current_item['price'] = float(line.split(':', 1)[1].strip().replace(',', '.').replace('¥', '') or 0)
        elif line.startswith('закупка:'): current_item['purchase'] = float(line.split(':', 1)[1].strip().replace(',', '.').replace('¥', '') or 0)
        elif line.startswith('доставка:'): current_item['delivery_factory'] = float(line.split(':', 1)[1].strip().replace(',', '.').replace('¥', '') or 0)
        elif line.startswith('размеры:'): 
            try: current_item['dims'] = tuple(map(float, line.split(':', 1)[1].strip().replace(',', '.').split()[:3]))
            except: pass
        elif line.startswith('курс клиенту:'): client_rate = float(line.split(':', 1)[1].strip().replace(',', '.'))
        elif line.startswith('мой курс:'): real_rate = float(line.split(':', 1)[1].strip().replace(',', '.'))

    if current_item and current_item.get('name'): items.append(current_item)
    if not items: return await update.message.reply_text('❌ Ошибка парсинга товаров.')

    total_price_cny = sum(i['price'] * i['qty'] for i in items)
    total_delivery_cny = sum(i.get('delivery_factory', 0) for i in items)
    total_cny = total_price_cny + total_delivery_cny
    
    commission_cny_base = total_cny * 0.03
    commission_amd_calc = commission_cny_base * client_rate
    
    if commission_amd_calc < 10000:
        commission_amd = 10000
        commission_cny_display = 10000 / client_rate
    else:
        commission_amd = int(commission_amd_calc)
        commission_cny_display = commission_cny_base

    total_with_fee_cny = total_cny + commission_cny_display
    final_total_amd = int((total_cny * client_rate) + commission_amd)
    
    orders[uid] = {
        'type': 'paste', 'client': client, 'items': items,
        'client_rate': client_rate, 'real_rate': real_rate,
        'total_amd': final_total_amd, 'final_total_amd': final_total_amd,
        'commission_amd': commission_amd
    }

    items_list_str = "".join([f"• {i['name']} — {i['qty']} шт | {i['price'] * i['qty']:.1f}¥\n" for i in items])
    msg_client = f"""<b>COMMERCIAL INVOICE: {client.upper()}</b>
📅 Date: {datetime.now().strftime('%d.%m.%Y')}

<b>ТОВАРНАЯ ВЕДОМОСТЬ (Netto)</b>
{items_list_str.strip()}
<code>────────────────────────</code>
<b>Subtotal (Товар):</b> {total_price_cny:.1f}¥

<b>ЛОГИСТИЧЕСКИЕ РАСХОДЫ</b>
• Внутренняя логистика (China): {total_delivery_cny:.1f}¥
• Проверка и упаковка (Processing): Включено
<code>────────────────────────</code>
<b>Total Logistics:</b> {total_delivery_cny:.1f}¥

<b>КОМИССИЯ И СЕРВИС (Service Fee)</b>
• {commission_cny_display:.1f}¥

<b>ИТОГОВЫЙ РАСЧЕТ (Convertation)</b>
• Всего в юанях: {total_with_fee_cny:.1f}¥
• Курс обмена (Exchange): {client_rate}

✅ <b>ИТОГО К ОПЛАТЕ: {final_total_amd:,} AMD</b>"""

    purchase_cny = sum(i.get('purchase', 0) * i['qty'] for i in items)
    real_expenses_amd = int((purchase_cny + total_delivery_cny) * real_rate)
    profit_amd = final_total_amd - real_expenses_amd

    msg_admin = f"""💼 <b>ВНУТРЕННИЙ РАСЧЕТ: {client.upper()}</b>

<b>РАСХОДЫ (Курс закупа: {real_rate}):</b>
• Закупка товара: {purchase_cny:.1f}¥
• Доставка по Китаю: {total_delivery_cny:.1f}¥
Итого расход: <b>{real_expenses_amd:,} AMD</b>

<b>ДОХОДЫ:</b>
• Взяли с клиента: <b>{final_total_amd:,} AMD</b>
• Комиссия в чеке: {commission_amd:,} AMD

💰 <b>ЧИСТАЯ ПРИБЫЛЬ: {profit_amd:,} AMD</b>\n
Напиши /ff или /dostavka для продолжения."""

    await update.message.reply_text(msg_client, parse_mode='HTML')

    client_orders, _ = await get_client_orders_from_notion(client)
    keyboard = []
    
    if client_orders:
        orders[uid]['existing_notion_page_id'] = client_orders[0]['id']
        last_date = client_orders[0].get('date', 'неизвестно')
        msg_admin += f"\n\n⚠️ <b>Клиент найден в базе!</b> (Заказ от: {last_date})"
        keyboard.append([InlineKeyboardButton("🔄 Обновить старый заказ", callback_data='paste_update')])
        keyboard.append([InlineKeyboardButton("➕ Сохранить как НОВЫЙ", callback_data='paste_new')])
    else:
        keyboard.append([InlineKeyboardButton("💾 Сохранить в Notion", callback_data='paste_new')])

    await update.message.reply_text(msg_admin, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))

async def paste_save_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); uid = str(update.effective_user.id)
    
    if query.data == 'paste_update':
        orders[uid]['notion_page_id'] = orders[uid].get('existing_notion_page_id')
        action = "Обновлен старый заказ"
    else:
        orders[uid]['notion_page_id'] = None
        action = "Создан новый заказ"
        
    url = await save_to_notion(uid)
    await query.edit_message_text(f"✅ {action}:\n{url}" if url else "⚠️ Ошибка Notion")
    save_session()

# --- /ZAKAZ КОМАНДА ---
Z_INVOICE, Z_NAME, Z_QTY, Z_PRICE, Z_PURCHASE, Z_DELIVERY, Z_DIMS, Z_MORE, Z_CLIENT_RATE, Z_REAL_RATE = range(10)

async def cmd_zakaz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if not context.args: return ConversationHandler.END
    raw_client = ' '.join(context.args)
    client = normalize_client_name(raw_client)
    orders[uid] = {'client': client, 'items': [], 'type': 'zakaz'}
    await update.message.reply_text(f"Клиент: {client}. Нужен инвойс?", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Да", callback_data='z_inv_yes'), InlineKeyboardButton("❌ Нет", callback_data='z_inv_no')]
    ]))
    return Z_INVOICE

async def z_invoice_cb(update, context):
    query = update.callback_query; await query.answer(); uid = str(update.effective_user.id)
    orders[uid]['invoice_needed'] = (query.data == 'z_inv_yes')
    await query.edit_message_text("Название товара:")
    return Z_NAME

async def z_get_name(update, context):
    uid = str(update.effective_user.id)
    orders[uid]['current'] = {'name': update.message.text.strip()}
    await update.message.reply_text("Количество:")
    return Z_QTY

async def z_get_qty(update, context):
    uid = str(update.effective_user.id)
    try:
        orders[uid]['current']['qty'] = int(update.message.text.strip())
        await update.message.reply_text("Цена клиенту (CNY):")
        return Z_PRICE
    except Exception:
        await update.message.reply_text("❌ Ошибка. Введи целое число для количества:")
        return Z_QTY

async def z_get_price(update, context):
    uid = str(update.effective_user.id)
    try:
        orders[uid]['current']['price'] = float(update.message.text.replace(',', '.').strip())
        await update.message.reply_text("Закупка (CNY):")
        return Z_PURCHASE
    except Exception:
        await update.message.reply_text("❌ Ошибка. Введи число (например 5.5):")
        return Z_PRICE

async def z_get_purchase(update, context):
    uid = str(update.effective_user.id)
    try:
        orders[uid]['current']['purchase'] = float(update.message.text.replace(',', '.').strip())
        await update.message.reply_text("Доставка до склада (CNY):")
        return Z_DELIVERY
    except Exception:
        await update.message.reply_text("❌ Ошибка. Введи число:")
        return Z_PURCHASE

async def z_get_delivery(update, context):
    uid = str(update.effective_user.id)
    try:
        orders[uid]['current']['delivery_factory'] = float(update.message.text.replace(',', '.').strip())
        await update.message.reply_text("Размеры (Д Ш В) или '-':")
        return Z_DIMS
    except Exception:
        await update.message.reply_text("❌ Ошибка. Введи число:")
        return Z_DELIVERY

async def z_get_dims(update, context):
    uid = str(update.effective_user.id); text = update.message.text.strip()
    if text == '-':
        orders[uid]['current']['dims'] = (0,0,0)
    else:
        try:
            orders[uid]['current']['dims'] = tuple(map(float, text.replace(',', '.').split()))
        except Exception:
            await update.message.reply_text("❌ Неверный формат. Введи 3 числа через пробел (например: 15 10 5) или '-':")
            return Z_DIMS
            
    orders[uid]['items'].append(orders[uid]['current'])
    await update.message.reply_text("Ещё товар?", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Да", callback_data='z_more_yes'), InlineKeyboardButton("❌ Нет", callback_data='z_more_no')]
    ]))
    return Z_MORE

async def z_more_cb(update, context):
    query = update.callback_query; await query.answer()
    if query.data == 'z_more_yes': 
        await query.edit_message_text("Название товара:")
        return Z_NAME
    await query.edit_message_text("Курс клиенту:")
    return Z_CLIENT_RATE

async def z_client_rate(update, context):
    uid = str(update.effective_user.id)
    try:
        orders[uid]['client_rate'] = float(update.message.text.replace(',', '.').strip())
        await update.message.reply_text("Реальный курс:")
        return Z_REAL_RATE
    except Exception:
        await update.message.reply_text("❌ Ошибка. Введи число:")
        return Z_CLIENT_RATE

async def z_real_rate(update, context):
    uid = str(update.effective_user.id)
    try:
        orders[uid]['real_rate'] = float(update.message.text.replace(',', '.').strip())
    except Exception:
        await update.message.reply_text("❌ Ошибка. Введи число:")
        return Z_REAL_RATE
        
    items = orders[uid]['items']
    client_rate = orders[uid]['client_rate']
    real_rate = orders[uid]['real_rate']
    client_name = orders[uid]['client']
    
    total_price_cny = sum(item['price'] * item['qty'] for item in items)
    total_delivery_cny = sum(item.get('delivery_factory', 0) for item in items)
    total_cny = total_price_cny + total_delivery_cny
    
    commission_cny_base = total_cny * 0.03
    commission_amd_calc = commission_cny_base * client_rate
    
    if commission_amd_calc < 10000:
        commission_amd = 10000
        commission_cny_display = 10000 / client_rate 
    else:
        commission_amd = int(commission_amd_calc)
        commission_cny_display = commission_cny_base

    total_with_fee_cny = total_cny + commission_cny_display
    final_total_amd = int((total_cny * client_rate) + commission_amd)
    
    orders[uid]['total_amd'] = final_total_amd
    orders[uid]['commission_amd'] = commission_amd

    items_list_str = "".join([f"• {i['name']} — {i['qty']} шт | {i['price'] * i['qty']:.1f}¥\n" for i in items])
    msg_client = f"""<b>COMMERCIAL INVOICE: {client_name.upper()}</b>
📅 Date: {datetime.now().strftime('%d.%m.%Y')}

<b>ТОВАРНАЯ ВЕДОМОСТЬ (Netto)</b>
{items_list_str.strip()}
<code>────────────────────────</code>
<b>Subtotal (Товар):</b> {total_price_cny:.1f}¥

<b>ЛОГИСТИЧЕСКИЕ РАСХОДЫ</b>
• Внутренняя логистика (China): {total_delivery_cny:.1f}¥
• Проверка и упаковка (Processing): Включено
<code>────────────────────────</code>
<b>Total Logistics:</b> {total_delivery_cny:.1f}¥

<b>КОМИССИЯ И СЕРВИС (Service Fee)</b>
• {commission_cny_display:.1f}¥

<b>ИТОГОВЫЙ РАСЧЕТ (Convertation)</b>
• Всего в юанях: {total_with_fee_cny:.1f}¥
• Курс обмена (Exchange): {client_rate}

✅ <b>ИТОГО К ОПЛАТЕ: {final_total_amd:,} AMD</b>"""

    purchase_cny = sum(i.get('purchase', 0) * i['qty'] for i in items)
    real_expenses_amd = int((purchase_cny + total_delivery_cny) * real_rate)
    profit_amd = final_total_amd - real_expenses_amd

    msg_admin = f"""💼 <b>ВНУТРЕННИЙ РАСЧЕТ: {client_name.upper()}</b>

<b>РАСХОДЫ (Курс закупа: {real_rate}):</b>
• Закупка товара: {purchase_cny:.1f}¥
• Доставка по Китаю: {total_delivery_cny:.1f}¥
Итого расход: <b>{real_expenses_amd:,} AMD</b>

<b>ДОХОДЫ:</b>
• Взяли с клиента: <b>{final_total_amd:,} AMD</b>
• Комиссия в чеке: {commission_amd:,} AMD

💰 <b>ЧИСТАЯ ПРИБЫЛЬ: {profit_amd:,} AMD</b>"""

    await update.message.reply_text(msg_client, parse_mode='HTML')
    await update.message.reply_text(msg_admin, parse_mode='HTML')
    
    url = await save_to_notion(uid)
    await update.message.reply_text(f"✅ Сохранено:\n{url}" if url else "⚠️ Ошибка Notion")
    save_session()
    return ConversationHandler.END

# --- /FF ПОЛНАЯ КОМАНДА ---
F_MAIN_MENU, F_SINGLE_ITEMS, F_SINGLE_DIMS, F_BUNDLE_CREATE, F_BUNDLE_NAME, F_BUNDLE_DIMS, F_BUNDLE_PACKAGE, F_BUNDLE_THERMAL, F_BUNDLE_WORK, F_BOX_PRICE, F_SUMMARY = range(20, 31)

async def cmd_ff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if uid not in orders or not orders[uid].get('items'): 
        await update.message.reply_text("Сначала /zakaz или /paste"); return ConversationHandler.END
    
    orders[uid]['ff_bundles'] = orders[uid].get('ff_bundles', [])
    orders[uid]['ff_single_items'] = orders[uid].get('ff_single_items', [])
    orders[uid]['ff_items_in_bundles'] = orders[uid].get('ff_items_in_bundles', set())
    
    return await show_ff_main_menu(update, uid)

async def show_ff_main_menu(update_or_query, uid):
    items = orders[uid]['items']
    items_in_bundles = orders[uid]['ff_items_in_bundles']
    bundles = orders[uid]['ff_bundles']
    
    msg = "📦 <b>FF Китай — Выбор режима</b>\n\n<b>Товары:</b>\n"
    for idx, item in enumerate(items):
        if idx in items_in_bundles: msg += f"☑️ <s>{item['name']}</s> (в наборе)\n"
        else: msg += f"☐ {item['name']}\n"
    
    msg += f"\n<b>Создано наборов:</b> {len(bundles)}\n"
    for b in bundles: msg += f"  📦 {b.get('name', 'Без имени')} (Кол-во: {b.get('qty', 1)})\n"
    
    keyboard = [
        [InlineKeyboardButton("📦 Считать по одиночке", callback_data='ff_mode_single')],
        [InlineKeyboardButton("📦 Собрать набор", callback_data='ff_mode_bundle')],
        [InlineKeyboardButton("✅ Продолжить →", callback_data='ff_mode_continue')],
    ]
    if hasattr(update_or_query, 'edit_message_text'):
        await update_or_query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    else:
        await update_or_query.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    return F_MAIN_MENU

async def ff_main_menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); uid = str(update.effective_user.id)
    
    if query.data == 'ff_mode_single':
        available = [(idx, item) for idx, item in enumerate(orders[uid]['items']) if idx not in orders[uid]['ff_items_in_bundles']]
        if not available:
            await query.answer("Нет товаров для одиночных", show_alert=True); return F_MAIN_MENU
        orders[uid]['ff_single_available'] = available
        orders[uid]['ff_single_index'] = 0
        
        query_mock = type('obj', (object,), {'edit_message_text': query.edit_message_text})
        return await show_single_item(query_mock, uid)
        
    elif query.data == 'ff_mode_bundle':
        available = [(idx, item) for idx, item in enumerate(orders[uid]['items']) if idx not in orders[uid]['ff_items_in_bundles']]
        if not available:
            await query.answer("Нет товаров для набора", show_alert=True); return F_MAIN_MENU
        orders[uid]['ff_bundle_selected'] = set()
        orders[uid]['ff_bundle_available'] = available
        
        query_mock = type('obj', (object,), {'edit_message_text': query.edit_message_text})
        return await show_bundle_item_selection(query_mock, uid)
        
    elif query.data == 'ff_mode_continue':
        await query.edit_message_text("📦 <b>Стоимость коробки</b>\n\nНапиши цену за 1 коробку 60x40x40 (¥):", parse_mode='HTML')
        return F_BOX_PRICE

async def ff_box_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try:
        box_price = float(update.message.text.replace(',', '.'))
        orders[uid]['custom_box_price'] = box_price
        
        bundles = orders[uid].get('ff_bundles', [])
        single_items_list = [s['item'] for s in orders[uid].get('ff_single_items', [])]
        
        bundle_items_for_box = [{'name': b['name'], 'dims': b['dims'], 'qty': b.get('qty', 1)} for b in bundles]
        
        bundle_boxes = optimize_boxes(bundle_items_for_box)
        single_boxes = optimize_boxes(single_items_list)
        total_boxes = bundle_boxes + single_boxes
        
        orders[uid]['ff_boxes_total'] = total_boxes * box_price
        
        await update.message.reply_text(f"Коробок: {total_boxes}. Стоимость коробок: {total_boxes * box_price}¥\n\nНапиши стоимость сборки/работы для ВСЕХ одиночных товаров суммарно (¥) или 0:")
        return F_SUMMARY
    except Exception:
        await update.message.reply_text("❌ Ошибка. Введи число (например 12.5):")
        return F_BOX_PRICE

# -- Логика одиночных --
async def show_single_item(update_or_query, uid):
    idx = orders[uid]['ff_single_index']
    available = orders[uid]['ff_single_available']
    if idx >= len(available): 
        return await show_ff_main_menu(update_or_query, uid)
    
    item_idx, item = available[idx]
    l, w, h = item.get('dims', (0,0,0))
    
    if (l, w, h) == (0, 0, 0):
        msg = f"⚠️ Товар: <b>{item['name']}</b>\n\nВведи размеры товара 1 шт (Д Ш В в см), чтобы бот посчитал, сколько влезет в коробку:\nНапример: 15 10 5"
        if hasattr(update_or_query, 'edit_message_text'):
            await update_or_query.edit_message_text(msg, parse_mode='HTML')
        else:
            await update_or_query.message.reply_text(msg, parse_mode='HTML')
        return F_SINGLE_DIMS
    
    packages = await get_packages_from_notion()
    orders[uid]['ff_available_packages'] = packages
    
    msg = f"📦 Товар {idx+1}/{len(available)}: <b>{item['name']}</b>\nРазмеры: {l}x{w}x{h} | Кол-во: {item['qty']}\n\nВыбери пакет для упаковки:"
    keyboard = [[InlineKeyboardButton(f"📦 {p['name']} — {p['price']}¥", callback_data=f'ff_s_pkg_{i}')] for i, p in enumerate(packages)]
    keyboard.append([InlineKeyboardButton("💰 Своя цена", callback_data='ff_s_custom')])
    
    if hasattr(update_or_query, 'edit_message_text'):
        await update_or_query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    else:
        await update_or_query.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    return F_SINGLE_ITEMS

async def ff_single_dims(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try:
        dims = tuple(map(float, update.message.text.replace(',', '.').split()))
        if len(dims) != 3: raise ValueError
        
        idx = orders[uid]['ff_single_index']
        item_idx, item = orders[uid]['ff_single_available'][idx]
        
        orders[uid]['ff_single_available'][idx][1]['dims'] = dims
        orders[uid]['items'][item_idx]['dims'] = dims
        
        query_mock = type('obj', (object,), {'message': update.message, 'reply_text': update.message.reply_text})
        return await show_single_item(query_mock, uid)
    except Exception:
        await update.message.reply_text("❌ Неверный формат. Введи 3 числа (например: 15 10 5):")
        return F_SINGLE_DIMS

async def ff_single_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); uid = str(update.effective_user.id)
    if query.data == 'ff_s_custom':
        await query.edit_message_text("Введи цену пакета (¥):"); return F_SINGLE_ITEMS
    
    pkg_idx = int(query.data.replace('ff_s_pkg_', ''))
    pkg = orders[uid]['ff_available_packages'][pkg_idx]
    
    idx = orders[uid]['ff_single_index']
    item_idx, item = orders[uid]['ff_single_available'][idx]
    orders[uid]['ff_single_items'].append({'item': item, 'pkg': pkg, 'total': pkg['price'] * item['qty'], 'qty': item['qty']})
    orders[uid]['ff_items_in_bundles'].add(item_idx)
    orders[uid]['ff_single_index'] += 1
    
    query_mock = type('obj', (object,), {'edit_message_text': query.edit_message_text})
    return await show_single_item(query_mock, uid)

async def ff_single_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try:
        price = float(update.message.text.replace(',', '.'))
    except Exception:
        await update.message.reply_text("❌ Ошибка. Введи число:")
        return F_SINGLE_ITEMS
        
    idx = orders[uid]['ff_single_index']
    item_idx, item = orders[uid]['ff_single_available'][idx]
    
    orders[uid]['ff_single_items'].append({'item': item, 'pkg': {'name': 'Ручной'}, 'total': price * item['qty'], 'qty': item['qty']})
    orders[uid]['ff_items_in_bundles'].add(item_idx)
    orders[uid]['ff_single_index'] += 1
    
    query_mock = type('obj', (object,), {'edit_message_text': update.message.reply_text})
    return await show_single_item(query_mock, uid)

# -- Логика наборов --
async def show_bundle_item_selection(update_or_query, uid):
    available = orders[uid]['ff_bundle_available']
    selected = orders[uid]['ff_bundle_selected']
    msg = "Выбери товары для набора:\n\n"
    
    keyboard = []
    for idx, (item_idx, item) in enumerate(available):
        mark = "☑️" if item_idx in selected else "☐"
        keyboard.append([InlineKeyboardButton(f"{mark} {item['name']} x {item['qty']}", callback_data=f'ff_b_sel_{item_idx}')])
    keyboard.append([InlineKeyboardButton("✅ Далее", callback_data='ff_b_next')])
    
    if hasattr(update_or_query, 'edit_message_text'):
        await update_or_query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update_or_query.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))
    return F_BUNDLE_CREATE

async def ff_bundle_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); uid = str(update.effective_user.id)
    if query.data.startswith('ff_b_sel_'):
        i_idx = int(query.data.replace('ff_b_sel_', ''))
        if i_idx in orders[uid]['ff_bundle_selected']: orders[uid]['ff_bundle_selected'].remove(i_idx)
        else: orders[uid]['ff_bundle_selected'].add(i_idx)
        
        query_mock = type('obj', (object,), {'edit_message_text': query.edit_message_text})
        return await show_bundle_item_selection(query_mock, uid)
    elif query.data == 'ff_b_next':
        if not orders[uid]['ff_bundle_selected']: return F_BUNDLE_CREATE
        await query.edit_message_text("Введи имя набора:"); return F_BUNDLE_NAME

async def ff_bundle_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id); orders[uid]['ff_b_name'] = update.message.text.strip()
    await update.message.reply_text("Размеры ОДНОГО готового набора (Д Ш В в см):\nЭти размеры нужны для расчета коробок"); return F_BUNDLE_DIMS

async def ff_bundle_dims(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try:
        orders[uid]['ff_b_dims'] = tuple(map(float, update.message.text.replace(',', '.').split()))
    except Exception:
        await update.message.reply_text("❌ Ошибка формата. Введи 3 числа (например: 16 12 5):")
        return F_BUNDLE_DIMS
        
    selected_indices = orders[uid].get('ff_bundle_selected', set())
    items = orders[uid]['items']
    
    if not selected_indices:
        bundle_qty = 1
    else:
        bundle_qty = min(items[i].get('qty', 1) for i in selected_indices)
        
    orders[uid]['ff_b_qty'] = bundle_qty
    await update.message.reply_text(f"🤖 Бот рассчитал: получается <b>{bundle_qty} наборов</b>.", parse_mode='HTML')
        
    packages = await get_packages_from_notion()
    orders[uid]['ff_available_packages'] = packages
    keyboard = [[InlineKeyboardButton(f"📦 {p['name']} — {p['price']}¥", callback_data=f'ff_b_pkg_{i}')] for i, p in enumerate(packages)]
    keyboard.append([InlineKeyboardButton("💰 Своя цена", callback_data='ff_b_custom')])
    
    await update.message.reply_text("Выбери пакет для упаковки 1 набора:", reply_markup=InlineKeyboardMarkup(keyboard))
    return F_BUNDLE_PACKAGE

async def ff_bundle_package_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); uid = str(update.effective_user.id)
    if query.data == 'ff_b_custom':
        await query.edit_message_text("Цена пакета (¥):"); return F_BUNDLE_PACKAGE
    
    pkg_idx = int(query.data.replace('ff_b_pkg_', ''))
    orders[uid]['ff_b_pkg'] = orders[uid]['ff_available_packages'][pkg_idx]
    await query.edit_message_text("Кол-во термобумаги на 1 набор (листов) или 'auto':"); return F_BUNDLE_THERMAL

async def ff_bundle_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try:
        orders[uid]['ff_b_pkg'] = {'name': 'Ручной', 'price': float(update.message.text.replace(',', '.'))}
    except Exception:
        await update.message.reply_text("❌ Ошибка. Введи число:")
        return F_BUNDLE_PACKAGE
    await update.message.reply_text("Кол-во термобумаги на 1 набор (листов) или 'auto':"); return F_BUNDLE_THERMAL

async def ff_bundle_thermal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id); txt = update.message.text.strip().lower()
    try:
        sheets = 1.0 if txt == 'auto' else float(txt.replace(',', '.'))
    except Exception:
        await update.message.reply_text("❌ Ошибка. Введи число или 'auto':")
        return F_BUNDLE_THERMAL
    orders[uid]['ff_b_thermal'] = sheets * 0.016
    await update.message.reply_text("Цена сборки ЗА 1 НАБОР (¥):"); return F_BUNDLE_WORK

async def ff_bundle_work(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try:
        work = float(update.message.text.replace(',', '.'))
    except Exception:
        await update.message.reply_text("❌ Ошибка. Введи число:")
        return F_BUNDLE_WORK
    
    qty = orders[uid]['ff_b_qty']
    b_total = (orders[uid]['ff_b_pkg']['price'] + orders[uid]['ff_b_thermal'] + work) * qty
    
    orders[uid]['ff_bundles'].append({
        'name': orders[uid]['ff_b_name'],
        'dims': orders[uid]['ff_b_dims'],
        'qty': qty,
        'pkg': orders[uid]['ff_b_pkg'],
        'thermal': orders[uid]['ff_b_thermal'],
        'work_price': work,
        'total': b_total,
        'item_indices': list(orders[uid]['ff_bundle_selected'])
    })
    
    orders[uid]['ff_items_in_bundles'].update(orders[uid]['ff_bundle_selected'])
    
    query_mock = type('obj', (object,), {'edit_message_text': update.message.reply_text})
    return await show_ff_main_menu(query_mock, uid)

async def ff_summary_work(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try:
        single_work = float(update.message.text.replace(',', '.'))
    except Exception:
        await update.message.reply_text("❌ Ошибка. Введи число:")
        return F_SUMMARY
    
    bundles = orders[uid].get('ff_bundles', [])
    single_items = orders[uid].get('ff_single_items', [])
    
    bundles_total = sum(b['total'] for b in bundles)
    single_total = sum(s['total'] for s in single_items)
    boxes_total = orders[uid].get('ff_boxes_total', 0)
    
    ff_total = bundles_total + single_total + boxes_total + single_work
    orders[uid]['ff_total_yuan'] = ff_total
    
    url = await save_to_notion(uid)
    await update.message.reply_text(f"✅ Итог FF: {ff_total}¥\nСохранено: {url}")
    save_session()
    return ConversationHandler.END

# --- /DOSTAVKA КОМАНДА ---
D_WAREHOUSE, D_BOXES, D_MORE_WH, D_RUB_RATE = range(30, 34)

async def cmd_dostavka(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if uid not in orders: await update.message.reply_text("Сначала /zakaz"); return ConversationHandler.END
    orders[uid]['warehouses'] = []
    keyboard = [[InlineKeyboardButton(c, callback_data=f'd_wh_{c}')] for c in TARIFFS.keys()]
    await update.message.reply_text("Выбери склад РФ:", reply_markup=InlineKeyboardMarkup(keyboard))
    return D_WAREHOUSE

async def d_warehouse_cb(update, context):
    query = update.callback_query; await query.answer(); uid = str(update.effective_user.id)
    orders[uid]['current_wh'] = query.data.replace('d_wh_', '')
    await query.edit_message_text("Количество коробок:"); return D_BOXES

async def d_boxes(update, context):
    uid = str(update.effective_user.id)
    try:
        boxes = int(update.message.text.strip())
    except Exception:
        await update.message.reply_text("❌ Ошибка. Введи целое число:")
        return D_BOXES
        
    city = orders[uid]['current_wh']
    
    if city == 'Свой тариф':
        await update.message.reply_text("Этот склад требует ручного тарифа. Выбери другой через /dostavka"); return ConversationHandler.END
        
    cost = TARIFFS[city] * boxes
    orders[uid]['warehouses'].append({'city': city, 'boxes': boxes, 'cost': cost})
    
    await update.message.reply_text("Еще склад?", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("Да", callback_data='d_more_yes'), InlineKeyboardButton("Нет", callback_data='d_more_no')]
    ]))
    return D_MORE_WH

async def d_more_cb(update, context):
    query = update.callback_query; await query.answer()
    if query.data == 'd_more_yes':
        keyboard = [[InlineKeyboardButton(c, callback_data=f'd_wh_{c}')] for c in TARIFFS.keys()]
        await query.edit_message_text("Склад РФ:", reply_markup=InlineKeyboardMarkup(keyboard)); return D_WAREHOUSE
    await query.edit_message_text("Курс ₽→драм:")
    return D_RUB_RATE

async def d_rub_rate(update, context):
    uid = str(update.effective_user.id)
    try:
        orders[uid]['rub_rate'] = float(update.message.text.replace(',', '.'))
    except Exception:
        await update.message.reply_text("❌ Ошибка. Введи число:")
        return D_RUB_RATE
        
    total_rub = sum(w['cost'] for w in orders[uid]['warehouses']) + 7000 # 7000 - IOB pickup
    orders[uid]['fillx_total'] = total_rub
    
    url = await save_to_notion(uid)
    await update.message.reply_text(f"✅ Доставка FILLX: {total_rub}₽\nСохранено: {url}")
    save_session(); return ConversationHandler.END

# ======== MAIN ЗАПУСК ========
def main():
    load_session()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('paste', cmd_paste))
    app.add_handler(CallbackQueryHandler(paste_save_cb, pattern='^paste_new$|^paste_update$'))
    
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler('zakaz', cmd_zakaz)],
        states={
            Z_INVOICE: [CallbackQueryHandler(z_invoice_cb)],
            Z_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, z_get_name)],
            Z_QTY: [MessageHandler(filters.TEXT & ~filters.COMMAND, z_get_qty)],
            Z_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, z_get_price)],
            Z_PURCHASE: [MessageHandler(filters.TEXT & ~filters.COMMAND, z_get_purchase)],
            Z_DELIVERY: [MessageHandler(filters.TEXT & ~filters.COMMAND, z_get_delivery)],
            Z_DIMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, z_get_dims)],
            Z_MORE: [CallbackQueryHandler(z_more_cb)],
            Z_CLIENT_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, z_client_rate)],
            Z_REAL_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, z_real_rate)],
        },
        fallbacks=[CommandHandler('cancel', lambda u,c: ConversationHandler.END)],
    ))

    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler('ff', cmd_ff)],
        states={
            F_MAIN_MENU: [CallbackQueryHandler(ff_main_menu_cb)],
            F_SINGLE_ITEMS: [CallbackQueryHandler(ff_single_cb), MessageHandler(filters.TEXT & ~filters.COMMAND, ff_single_price)],
            F_SINGLE_DIMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, ff_single_dims)],
            F_BUNDLE_CREATE: [CallbackQueryHandler(ff_bundle_cb)],
            F_BUNDLE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ff_bundle_name)],
            F_BUNDLE_DIMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, ff_bundle_dims)],
            F_BUNDLE_PACKAGE: [CallbackQueryHandler(ff_bundle_package_cb), MessageHandler(filters.TEXT & ~filters.COMMAND, ff_bundle_price)],
            F_BUNDLE_THERMAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, ff_bundle_thermal)],
            F_BUNDLE_WORK: [MessageHandler(filters.TEXT & ~filters.COMMAND, ff_bundle_work)],
            F_BOX_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ff_box_price)],
            F_SUMMARY: [MessageHandler(filters.TEXT & ~filters.COMMAND, ff_summary_work)],
        },
        fallbacks=[CommandHandler('cancel', lambda u,c: ConversationHandler.END)]
    ))

    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler('dostavka', cmd_dostavka)],
        states={
            D_WAREHOUSE: [CallbackQueryHandler(d_warehouse_cb)],
            D_BOXES: [MessageHandler(filters.TEXT & ~filters.COMMAND, d_boxes)],
            D_MORE_WH: [CallbackQueryHandler(d_more_cb)],
            D_RUB_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, d_rub_rate)]
        },
        fallbacks=[CommandHandler('cancel', lambda u,c: ConversationHandler.END)]
    ))

    logger.info("Бот запущен. Версия v47 (Crash Fixed, Auto-Name Normalizer)")
    app.run_polling()

if __name__ == '__main__': main()
