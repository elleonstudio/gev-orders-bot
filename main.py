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

# ======== НАСТРОЙКИ И ЛОГИРОВАНИЕ ========
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
NOTION_TOKEN = os.getenv('NOTION_TOKEN')
NOTION_DATABASE_ID = "3278c4d1fb0e80c4b6e5f261d0631ed2"
PACKAGES_DATABASE_ID = "32a8c4d1fb0e806ebb98f5995704d0e5"

notion = Client(auth=NOTION_TOKEN) if NOTION_TOKEN else None

FF_BOX_PRICE = 2  # ¥ за коробку
orders = {}
TARIFFS = {
    'Коледино': 350, 'Невинномысск': 1100, 'Электросталь': 400, 'Белые Столбы': 350,
    'Чашниково': 350, 'Санкт-Петербург': 450, 'Казань': 450, 'Екатеринбург': 700,
    'Новосибирск': 850, 'Владивосток': 1000, 'Краснодар': 550
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

def fmt(n): return int(n) if n == int(n) else n

def get_code(client):
    return f"{client.upper().replace(' ', '-')}-{datetime.now().strftime('%y%m%d')}"

def calculate_boxes(l, w, h, qty):
    if l <= 0: return 0, 0
    MAX_L, MAX_W, MAX_H = 60, 40, 40
    i_l, i_w, i_h = max(1, int(MAX_L//l)), max(1, int(MAX_W//w)), max(1, int(MAX_H//h))
    per_box = i_l * i_w * i_h
    return per_box, math.ceil(qty / per_box)

def optimize_boxes(items):
    MAX_L, MAX_W, MAX_H = 60, 40, 40
    BOX_VOLUME = MAX_L * MAX_W * MAX_H
    all_items = []
    for item in items:
        l, w, h = item.get('dims', (0,0,0))
        volume = l * w * h
        for _ in range(item.get('qty', 0)):
            all_items.append({'name': item['name'], 'dims': (l, w, h), 'volume': volume})
    
    all_items.sort(key=lambda x: x['volume'], reverse=True)
    boxes = []
    
    for item in all_items:
        l, w, h = item['dims']
        placed = False
        for box in boxes:
            if box['remaining_volume'] >= item['volume']:
                can_fit = False
                for rot_l, rot_w, rot_h in [(l,w,h), (l,h,w), (w,l,h), (w,h,l), (h,l,w), (h,w,l)]:
                    if rot_l <= (MAX_L - box['used_l']) and rot_w <= (MAX_W - box['used_w']) and rot_h <= (MAX_H - box['used_h']):
                        can_fit = True
                        box['used_l'] += rot_l; box['used_w'] += rot_w; box['used_h'] += rot_h
                        break
                if can_fit:
                    box['items'].append(item); box['remaining_volume'] -= item['volume']; placed = True; break
        if not placed:
            boxes.append({'items': [item], 'remaining_volume': BOX_VOLUME - item['volume'], 'used_l': l, 'used_w': w, 'used_h': h})
    return boxes

# ======== NOTION API ========
async def get_packages_from_notion():
    if not notion or not PACKAGES_DATABASE_ID: return []
    try:
        res = notion.databases.query(database_id=PACKAGES_DATABASE_ID)
        packages = []
        for page in res.get('results', []):
            props = page['properties']
            name = props.get('Название', {}).get('title', [{}])[0].get('text', {}).get('content', '')
            price = props.get('Цена', {}).get('number', 0)
            l, w, h = props.get('Длина', {}).get('number', 0), props.get('Ширина', {}).get('number', 0), props.get('Высота', {}).get('number', 0)
            if name and price:
                packages.append({'name': name, 'price': price, 'l': l, 'w': w, 'h': h, 'volume': l*w*h})
        return packages
    except: return []

async def get_client_orders_from_notion(client_name):
    if not notion or not NOTION_DATABASE_ID: return None, "Notion не настроен"
    try:
        res = notion.databases.query(database_id=NOTION_DATABASE_ID, sorts=[{"timestamp": "created_time", "direction": "descending"}], page_size=100)
        client_name_lower = client_name.lower()
        filtered = [p for p in res.get('results', []) if p['properties'].get('Клиент', {}).get('select', {}).get('name', '').lower() == client_name_lower]
        
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
            "Клиент": {"select": {"name": client}},
            "Описание товара": {"rich_text": [{"text": {"content": desc_text}}]},
            "Количество": {"number": float(total_qty)},
            "Цена клиенту (CNY)": {"number": float(total_price_client_cny)},
            "Цена закупки (CNY)": {"number": float(total_purchase_cny)},
            "Доставка (CNY)": {"number": float(delivery_cny)},
            "Курс клиенту": {"number": float(client_rate)},
            "Курс реальный": {"number": float(real_rate)},
            "Закупка реальная (AMD)": {"number": float(purchase_real_amd)},
            " На закупку (AMD)": {"number": float(purchase_real_amd)},
            "На закупку (CNY)": {"number": float(total_purchase_cny + delivery_cny)},
            " К ОПЛАТЕ (AMD)": {"number": float(total_amd)},
            "  К ОПЛАТЕ ": {"number": float(total_amd)},
            " Инвойс": {"select": {"name": "Да" if data.get('invoice_needed') else "Нет"}},
            "Статус": {"select": {"name": "Новый"}},
            "Date": {"date": {"start": datetime.now().strftime('%Y-%m-%d')}}
        }

        if 'ff_total_yuan' in data: properties["ИТОГО (CNY)"] = {"number": float(data['ff_total_yuan'])}
        if 'rub_rate' in data: properties["Курс ₽→драм"] = {"number": float(data['rub_rate'])}

        page_id = data.get('notion_page_id')
        if page_id: res = notion.pages.update(page_id=page_id, properties=properties)
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
        await update.message.reply_text('Вставь расчёт. Пример:\nКлиент: Имя\nТовар 1:\nНазвание: Крепление\nКоличество: 400\nЦена клиенту: 6.2\nЗакупка: 4.5\nДоставка: 43.6\nРазмеры: 16 12 5\nКурс клиенту: 58\nМой курс: 55\nКомиссия: 3%')
        return
        
    lines = text.strip().split('\n')
    client = 'Unknown'; items = []; client_rate = 58; real_rate = 55; current_item = None
    
    for line in lines:
        line = line.strip().lower()
        if not line: continue
        if line.startswith('клиент:'): client = line.split(':', 1)[1].strip().title()
        elif line.startswith('товар'):
            if current_item and current_item.get('name'): items.append(current_item)
            current_item = {'dims': (0,0,0)}
        elif line.startswith('название:'): 
            if current_item is None: current_item = {'dims': (0,0,0)}
            current_item['name'] = line.split(':', 1)[1].strip().title()
        elif line.startswith('количество:'): current_item['qty'] = int(line.split(':', 1)[1].strip() or 0)
        elif line.startswith('цена клиенту:'): current_item['price'] = float(line.split(':', 1)[1].strip().replace('¥', '') or 0)
        elif line.startswith('закупка:'): current_item['purchase'] = float(line.split(':', 1)[1].strip().replace('¥', '') or 0)
        elif line.startswith('доставка:'): current_item['delivery_factory'] = float(line.split(':', 1)[1].strip().replace('¥', '') or 0)
        elif line.startswith('размеры:'): 
            try: current_item['dims'] = tuple(map(float, line.split(':', 1)[1].strip().split()[:3]))
            except: pass
        elif line.startswith('курс клиенту:'): client_rate = float(line.split(':', 1)[1].strip())
        elif line.startswith('мой курс:'): real_rate = float(line.split(':', 1)[1].strip())

    if current_item and current_item.get('name'): items.append(current_item)
    if not items: return await update.message.reply_text('❌ Ошибка парсинга товаров.')

    # Логика математики
    total_price_cny = sum(i['price'] * i['qty'] for i in items)
    total_delivery_cny = sum(i.get('delivery_factory', 0) for i in items)
    total_cny = total_price_cny + total_delivery_cny
    
    # Правило 10000 AMD [cite: 4]
    commission_cny_base = total_cny * 0.03
    commission_amd_calc = commission_cny_base * client_rate
    
    if commission_amd_calc < 10000:
        commission_amd = 10000
        commission_cny_display = round(10000 / client_rate, 2)
    else:
        commission_amd = int(commission_amd_calc)
        commission_cny_display = commission_cny_base

    total_with_fee_cny = total_cny + commission_cny_display
    final_total_amd = int((total_cny * client_rate) + commission_amd)
    
    # Сохранение в сессию
    orders[uid] = {
        'type': 'paste', 'client': client, 'items': items,
        'client_rate': client_rate, 'real_rate': real_rate,
        'total_amd': final_total_amd, 'final_total_amd': final_total_amd,
        'commission_amd': commission_amd
    }

    # Чек для клиента 
    items_list_str = "".join([f"• {i['name']} — {i['qty']} шт | {i['price'] * i['qty']:.1f}¥\n" for i in items])
    msg_client = f"""<b>COMMERCIAL INVOICE: {client.upper()}</b>
📅 Date: {datetime.now().strftime('%d.%m.%Y')}

<b>1. ТОВАРНАЯ ВЕДОМОСТЬ (Netto)</b>
{items_list_str.strip()}
<code>────────────────────────</code>
<b>Subtotal (Товар):</b> {total_price_cny:.1f}¥

<b>2. ЛОГИСТИЧЕСКИЕ РАСХОДЫ</b>
• Внутренняя логистика (China): {total_delivery_cny:.1f}¥
• Проверка и упаковка (Processing): Включено
<code>────────────────────────</code>
<b>Total Logistics:</b> {total_delivery_cny:.1f}¥

<b>3. КОМИССИЯ И СЕРВИС (Service Fee)</b>
• {commission_cny_display:.1f}¥

<b>4. ИТОГОВЫЙ РАСЧЕТ (Convertation)</b>
• Всего в юанях: {total_with_fee_cny:.1f}¥
• Курс обмена (Exchange): {client_rate}

✅ <b>ИТОГО К ОПЛАТЕ: {final_total_amd:,} AMD</b>"""

    # Чек для админа
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
    await update.message.reply_text(msg_admin, parse_mode='HTML', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💾 Сохранить в Notion", callback_data='paste_save')]]))

async def paste_save_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); uid = str(update.effective_user.id)
    url = await save_to_notion(uid)
    await query.edit_message_text(f"✅ Сохранено:\n{url}" if url else "⚠️ Ошибка Notion")
    save_session()

# --- /ZAKAZ КОМАНДА ---
Z_INVOICE, Z_NAME, Z_QTY, Z_PRICE, Z_PURCHASE, Z_DELIVERY, Z_DIMS, Z_MORE, Z_CLIENT_RATE, Z_REAL_RATE = range(10)

async def cmd_zakaz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if not context.args: return ConversationHandler.END
    client = ' '.join(context.args)
    orders[uid] = {'client': client, 'items': [], 'type': 'zakaz'}
    await update.message.reply_text(f"Клиент: {client}. Нужен инвойс?", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Да", callback_data='z_inv_yes'), InlineKeyboardButton("❌ Нет", callback_data='z_inv_no')]
    ]))
    return Z_INVOICE

async def z_invoice_cb(update, context):
    query = update.callback_query; await query.answer(); uid = str(update.effective_user.id)
    orders[uid]['invoice_needed'] = (query.data == 'z_inv_yes')
    await query.edit_message_text("Название товара:"); return Z_NAME

async def z_get_name(update, context):
    uid = str(update.effective_user.id); orders[uid]['current'] = {'name': update.message.text.strip()}
    await update.message.reply_text("Количество:"); return Z_QTY

async def z_get_qty(update, context):
    uid = str(update.effective_user.id); orders[uid]['current']['qty'] = int(update.message.text)
    await update.message.reply_text("Цена клиенту (CNY):"); return Z_PRICE

async def z_get_price(update, context):
    uid = str(update.effective_user.id); orders[uid]['current']['price'] = float(update.message.text)
    await update.message.reply_text("Закупка (CNY):"); return Z_PURCHASE

async def z_get_purchase(update, context):
    uid = str(update.effective_user.id); orders[uid]['current']['purchase'] = float(update.message.text)
    await update.message.reply_text("Доставка до склада (CNY):"); return Z_DELIVERY

async def z_get_delivery(update, context):
    uid = str(update.effective_user.id); orders[uid]['current']['delivery_factory'] = float(update.message.text)
    await update.message.reply_text("Размеры (Д Ш В) или '-':"); return Z_DIMS

async def z_get_dims(update, context):
    uid = str(update.effective_user.id); text = update.message.text.strip()
    orders[uid]['current']['dims'] = (0,0,0) if text == '-' else tuple(map(float, text.split()))
    orders[uid]['items'].append(orders[uid]['current'])
    await update.message.reply_text("Ещё товар?", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Да", callback_data='z_more_yes'), InlineKeyboardButton("❌ Нет", callback_data='z_more_no')]
    ]))
    return Z_MORE

async def z_more_cb(update, context):
    query = update.callback_query; await query.answer()
    if query.data == 'z_more_yes': await query.edit_message_text("Название товара:"); return Z_NAME
    await query.edit_message_text("Курс клиенту:"); return Z_CLIENT_RATE

async def z_client_rate(update, context):
    uid = str(update.effective_user.id); orders[uid]['client_rate'] = float(update.message.text)
    await update.message.reply_text("Реальный курс:"); return Z_REAL_RATE

async def z_real_rate(update, context):
    uid = str(update.effective_user.id)
    orders[uid]['real_rate'] = float(update.message.text)
    
    # Тот же блок математики 10000 AMD [cite: 4]
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

    # Чек клиента 
    items_list_str = "".join([f"• {i['name']} — {i['qty']} шт | {i['price'] * i['qty']:.1f}¥\n" for i in items])
    msg_client = f"""<b>COMMERCIAL INVOICE: {client_name.upper()}</b>
📅 Date: {datetime.now().strftime('%d.%m.%Y')}

<b>1. ТОВАРНАЯ ВЕДОМОСТЬ (Netto)</b>
{items_list_str.strip()}
<code>────────────────────────</code>
<b>Subtotal (Товар):</b> {total_price_cny:.1f}¥

<b>2. ЛОГИСТИЧЕСКИЕ РАСХОДЫ</b>
• Внутренняя логистика (China): {total_delivery_cny:.1f}¥
• Проверка и упаковка (Processing): Включено
<code>────────────────────────</code>
<b>Total Logistics:</b> {total_delivery_cny:.1f}¥

<b>3. КОМИССИЯ И СЕРВИС (Service Fee)</b>
• {commission_cny_display:.1f}¥

<b>4. ИТОГОВЫЙ РАСЧЕТ (Convertation)</b>
• Всего в юанях: {total_with_fee_cny:.1f}¥
• Курс обмена (Exchange): {client_rate}

✅ <b>ИТОГО К ОПЛАТЕ: {final_total_amd:,} AMD</b>"""

    # Чек админа
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

# --- /FF КОМАНДА ---
F_MAIN_MENU, F_SINGLE_ITEMS, F_BUNDLE_NAME, F_BUNDLE_DIMS, F_BUNDLE_WORK = range(20, 25)

async def cmd_ff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if uid not in orders or not orders[uid].get('items'): 
        await update.message.reply_text("Сначала /zakaz или /paste"); return ConversationHandler.END
    
    orders[uid]['ff_bundles'] = orders[uid].get('ff_bundles', [])
    orders[uid]['ff_single_items'] = orders[uid].get('ff_single_items', [])
    
    await update.message.reply_text("📦 FF Режим:", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 Отдельно", callback_data='ff_single')],
        [InlineKeyboardButton("📦 Создать набор", callback_data='ff_bundle')],
        [InlineKeyboardButton("✅ Завершить FF", callback_data='ff_done')]
    ]))
    return F_MAIN_MENU

async def ff_menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); uid = str(update.effective_user.id)
    if query.data == 'ff_single':
        await query.edit_message_text("Режим одиночных товаров (В разработке. Нажми /ff для возврата)"); return ConversationHandler.END
    elif query.data == 'ff_bundle':
        await query.edit_message_text("Введи имя набора:"); return F_BUNDLE_NAME
    elif query.data == 'ff_done':
        # Простой расчет коробок
        boxes = len(optimize_boxes(orders[uid].get('items', [])))
        orders[uid]['ff_total_yuan'] = boxes * FF_BOX_PRICE
        url = await save_to_notion(uid)
        await query.edit_message_text(f"✅ FF завершен. Коробок: {boxes}. {url}")
        save_session(); return ConversationHandler.END

async def ff_bundle_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id); orders[uid]['current_bundle'] = {'name': update.message.text.strip(), 'items': []}
    await update.message.reply_text("Размеры набора (Д Ш В):"); return F_BUNDLE_DIMS

async def ff_bundle_dims(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id); orders[uid]['current_bundle']['dims'] = tuple(map(float, update.message.text.split()))
    await update.message.reply_text("Стоимость доставки набора (¥):"); return F_BUNDLE_WORK

async def ff_bundle_work(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id); orders[uid]['current_bundle']['shipping'] = float(update.message.text)
    orders[uid]['ff_bundles'].append(orders[uid]['current_bundle'])
    await update.message.reply_text("Набор сохранен. Напиши /ff чтобы продолжить.")
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
    boxes = int(update.message.text); city = orders[uid]['current_wh']
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
    await query.edit_message_text("Курс ₽→драм:"); return D_RUB_RATE

async def d_rub_rate(update, context):
    uid = str(update.effective_user.id); orders[uid]['rub_rate'] = float(update.message.text)
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
    app.add_handler(CallbackQueryHandler(paste_save_cb, pattern='^paste_save$'))
    
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
            F_MAIN_MENU: [CallbackQueryHandler(ff_menu_cb)],
            F_BUNDLE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ff_bundle_name)],
            F_BUNDLE_DIMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, ff_bundle_dims)],
            F_BUNDLE_WORK: [MessageHandler(filters.TEXT & ~filters.COMMAND, ff_bundle_work)]
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

    logger.info("Бот успешно запущен. Версия v41 (Full Architecture)")
    app.run_polling()

if __name__ == '__main__': main()
