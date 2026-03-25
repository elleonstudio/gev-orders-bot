import os
import logging
import math
import json
import traceback
import re
import io
import pandas as pd
from datetime import datetime
from notion_client import Client
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ConversationHandler, filters, ContextTypes

# ======== НАСТРОЙКИ ========
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
NOTION_TOKEN = os.getenv('NOTION_TOKEN')
NOTION_DATABASE_ID = "3278c4d1fb0e80c4b6e5f261d0631ed2"
PACKAGES_DATABASE_ID = "32a8c4d1fb0e806ebb98f5995704d0e5"

BOX_PRICE_CNY = 7.77
MAX_BOX_WEIGHT = 30.0  # Лимит веса на одну коробку

notion = Client(auth=NOTION_TOKEN) if NOTION_TOKEN else None
orders = {}

TARIFFS = {
    'Коледино': 350, 'Невинномысск': 1100, 'Электросталь': 400, 'Белые Столбы': 350,
    'Чашниково': 350, 'Санкт-Петербург': 450, 'Казань': 450, 'Екатеринбург': 700,
    'Новосибирск': 850, 'Владивосток': 1000, 'Краснодар': 550, 'Свой тариф': 0
}

# ======== УТИЛИТЫ ========
def normalize_client_name(name):
    return re.sub(r'\s+', '', name).strip().capitalize()

def get_code(client):
    return f"{client.upper()}-{datetime.now().strftime('%y%m%d')}"

def optimize_boxes_with_weight(items):
    """Алгоритм упаковки 3D + Вес (лимит 30кг)"""
    MAX_L, MAX_W, MAX_H = 60, 40, 40
    boxes = []
    
    all_units = []
    for item in items:
        for _ in range(item.get('qty', 0)):
            l, w, h = item.get('dims', (1,1,1))
            all_units.append({
                'name': item['name'],
                'dims': (l, w, h),
                'weight': item.get('weight', 0.0),
                'vol': l * w * h
            })

    for unit in all_units:
        placed = False
        for box in boxes:
            if box['rem_vol'] >= unit['vol'] and (box['cur_weight'] + unit['weight']) <= MAX_BOX_WEIGHT:
                box['items'].append(unit)
                box['rem_vol'] -= unit['vol']
                box['cur_weight'] += unit['weight']
                placed = True
                break
        if not placed:
            boxes.append({
                'items': [unit],
                'rem_vol': (MAX_L * MAX_W * MAX_H) - unit['vol'],
                'cur_weight': unit['weight']
            })
    return boxes

# ======== EXCEL ГЕНЕРАТОР ========
async def create_excel_invoice(uid):
    data = orders[uid]
    items_data = []
    for i, item in enumerate(data['items'], 1):
        delivery = item.get('delivery_factory', 0)
        items_data.append({
            "№": i, "Название товара": item['name'], "Кол-во (шт)": item['qty'],
            "Цена (¥)": item['price'], "Логистика (¥)": delivery, "Итого (¥)": (item['price'] * item['qty']) + delivery
        })
        
    items_data.extend([
        {"№": "", "Название товара": "", "Кол-во (шт)": "", "Цена (¥)": "", "Логистика (¥)": "", "Итого (¥)": ""},
        {"№": "", "Название товара": "", "Кол-во (шт)": "", "Цена (¥)": "", "Логистика (¥)": "SUBTOTAL:", "Итого (¥)": f"{data.get('total_cny_netto', 0):.1f} ¥"},
        {"№": "", "Название товара": "", "Кол-во (шт)": "", "Цена (¥)": "", "Логистика (¥)": "Комиссия (Мин. 10000 AMD):" if data.get('rule_applied') else "Комиссия (3%):", "Итого (¥)": f"{data.get('actual_comm_cny', 0):.1f} ¥"},
        {"№": "", "Название товара": "", "Кол-во (шт)": "", "Цена (¥)": "", "Логистика (¥)": "ИТОГО К ОПЛАТЕ:", "Итого (¥)": f"{data.get('final_total_amd', 0):,} AMD"}
    ])
    
    df = pd.DataFrame(items_data)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df.to_excel(writer, index=False, sheet_name='Invoice')
        worksheet = writer.sheets['Invoice']
        worksheet.set_column('A:A', 5)
        worksheet.set_column('B:B', 35)
        worksheet.set_column('C:C', 12)
        worksheet.set_column('D:E', 15)
        worksheet.set_column('F:F', 20)
    output.seek(0)
    return output

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
            if name and price > 0: packages.append({'name': name, 'price': price})
        return packages
    except: return []

async def get_client_orders_from_notion(client_name):
    if not notion or not NOTION_DATABASE_ID: return None, "Notion не настроен"
    try:
        res = notion.databases.query(database_id=NOTION_DATABASE_ID, sorts=[{"timestamp": "created_time", "direction": "descending"}], page_size=100)
        client_norm = normalize_client_name(client_name).lower()
        filtered = [p for p in res.get('results', []) if p['properties'].get('Клиент', {}).get('select', {}) and normalize_client_name(p['properties']['Клиент']['select'].get('name', '')).lower() == client_norm]
        if not filtered: return [], None
        return [{'id': p['id'], 'date': p.get('created_time', '')[:10]} for p in filtered[:5]], None
    except Exception as e: return None, str(e)

async def save_to_notion(uid):
    if not notion: return None
    try:
        data = orders[uid]
        properties = {
            "Код заказа": {"title": [{"text": {"content": get_code(data['client'])}}]},
            "Клиент": {"select": {"name": data['client']}},
            "Количество": {"number": float(sum(i['qty'] for i in data['items']))},
            " К ОПЛАТЕ (AMD)": {"number": float(data['final_total_amd'])},
            "Статус": {"select": {"name": "Новый"}},
            "Date": {"date": {"start": datetime.now().strftime('%Y-%m-%d')}}
        }
        if 'ff_total_yuan' in data: properties["ИТОГО (CNY)"] = {"number": float(data['ff_total_yuan'])}

        page_id = data.get('notion_page_id')
        if page_id: res = notion.pages.update(page_id=page_id, properties=properties)
        else:
            res = notion.pages.create(parent={"database_id": NOTION_DATABASE_ID}, properties=properties)
            orders[uid]['notion_page_id'] = res['id']
        return f"https://notion.so/{res['id'].replace('-', '')}"
    except: return None

# ======== ПАРСЕР /PASTE ========
def parse_paste_text(text):
    data = {'client': 'Unknown', 'items': [], 'client_rate': 58.0, 'real_rate': 55.0}
    current_item = None
    for line in text.split('\n'):
        l = line.strip().lower()
        if not l: continue
        if 'клиент:' in l: data['client'] = normalize_client_name(line.split(':', 1)[1])
        elif 'товар' in l:
            if current_item: data['items'].append(current_item)
            current_item = {'name': 'Товар', 'qty': 0, 'price': 0.0, 'purchase': 0.0, 'delivery_factory': 0.0, 'dims': (0,0,0), 'weight': 0.0}
        elif 'название:' in l: current_item['name'] = line.split(':', 1)[1].strip().title()
        elif 'количество:' in l: current_item['qty'] = int(re.findall(r'\d+', l)[0]) if re.findall(r'\d+', l) else 0
        elif 'цена клиенту:' in l: current_item['price'] = float(re.findall(r'\d+\.?\d*', l.replace(',', '.'))[0]) if re.findall(r'\d+\.?\d*', l.replace(',', '.')) else 0.0
        elif 'закупка:' in l: current_item['purchase'] = float(re.findall(r'\d+\.?\d*', l.replace(',', '.'))[0]) if re.findall(r'\d+\.?\d*', l.replace(',', '.')) else 0.0
        elif 'доставка:' in l: current_item['delivery_factory'] = float(re.findall(r'\d+\.?\d*', l.replace(',', '.'))[0]) if re.findall(r'\d+\.?\d*', l.replace(',', '.')) else 0.0
        elif 'размеры:' in l:
            nums = re.findall(r'\d+\.?\d*', l.replace(',', '.'))
            if len(nums) >= 3:
                current_item['dims'] = (float(nums[0]), float(nums[1]), float(nums[2]))
                if len(nums) >= 4: current_item['weight'] = float(nums[3])
        elif 'курс клиенту:' in l: data['client_rate'] = float(re.findall(r'\d+\.?\d*', l.replace(',', '.'))[0]) if re.findall(r'\d+\.?\d*', l.replace(',', '.')) else 58.0
        elif 'мой курс:' in l: data['real_rate'] = float(re.findall(r'\d+\.?\d*', l.replace(',', '.'))[0]) if re.findall(r'\d+\.?\d*', l.replace(',', '.')) else 55.0
    if current_item: data['items'].append(current_item)
    return data

async def cmd_paste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    text = update.message.text.replace('/paste', '').strip()
    if not text: return

    data = parse_paste_text(text)
    if not data['items']: return await update.message.reply_text("❌ Ошибка: товары не найдены.")

    subtotal_cny = sum((i['price'] * i['qty']) + i['delivery_factory'] for i in data['items'])
    comm_amd_3pct = (subtotal_cny * 0.03) * data['client_rate']
    
    rule_applied = comm_amd_3pct < 10000
    actual_comm_amd = 10000 if rule_applied else int(comm_amd_3pct)
    actual_comm_cny = 10000 / data['client_rate'] if rule_applied else subtotal_cny * 0.03

    final_total_amd = int((subtotal_cny * data['client_rate']) + actual_comm_amd)
    
    orders[uid] = data
    orders[uid].update({'final_total_amd': final_total_amd, 'total_cny_netto': subtotal_cny, 'rule_applied': rule_applied, 'actual_comm_cny': actual_comm_cny, 'ff_boxes_qty': 0})

    audit = [f"• Применено правило 10 000 AMD (3% было {int(comm_amd_3pct)} AMD)"] if rule_applied else []
    missing = [i['name'] for i in data['items'] if i['dims'] == (0,0,0)]
    if missing: audit.append(f"• У <b>{', '.join(missing)}</b> нет размеров. Спрошу в /ff.")
    if audit: await update.message.reply_text("⚠️ <b>Аудит расчета:</b>\n" + "\n".join(audit), parse_mode='HTML')

    inv_lines = "".join([f"• {i['name']} — {i['qty']} шт\n{i['qty']} × {i['price']} + {i['delivery_factory']} = {(i['price'] * i['qty']) + i['delivery_factory']:.1f}¥\n" for i in data['items']])
    
    msg_client = f"""<b>COMMERCIAL INVOICE: {data['client'].upper()}</b>
📅 Date: {datetime.now().strftime('%d.%m.%Y')}

<b>1. ТОВАРНАЯ ВЕДОМОСТЬ (Logistics Included)</b>
{inv_lines}<code>────────────────────────</code>
<b>SUBTOTAL:</b> {subtotal_cny:.1f}¥

<b>2. КОМИССИЯ И СЕРВИС (Service Fee)</b>
({'Минимальная 10000 AMD' if rule_applied else '3%'}): {actual_comm_cny:.1f}¥

<b>3. ИТОГОВЫЙ РАСЧЕТ (Convertation)</b>
• Всего в юанях: {subtotal_cny + actual_comm_cny:.1f}¥
• Курс: {data['client_rate']}

✅ <b>ИТОГО: {final_total_amd:,} AMD</b>"""

    await update.message.reply_text(msg_client, parse_mode='HTML')

    purchase_cny = sum(i.get('purchase', 0) * i['qty'] for i in data['items'])
    total_delivery_cny = sum(i.get('delivery_factory', 0) for i in data['items'])
    real_expenses_amd = int((purchase_cny + total_delivery_cny) * data['real_rate'])
    profit_amd = final_total_amd - real_expenses_amd

    msg_admin = f"""💼 <b>ВНУТРЕННИЙ РАСЧЕТ: {data['client'].upper()}</b>

<b>РАСХОДЫ (Курс закупа: {data['real_rate']}):</b>
• Закупка товара: {purchase_cny:.1f}¥
• Доставка по Китаю: {total_delivery_cny:.1f}¥
Итого расход: <b>{real_expenses_amd:,} AMD</b>

<b>ДОХОДЫ:</b>
• Взяли с клиента: <b>{final_total_amd:,} AMD</b>
• Комиссия в чеке: {actual_comm_amd:,} AMD

💰 <b>ЧИСТАЯ ПРИБЫЛЬ: {profit_amd:,} AMD</b>"""

    client_orders, _ = await get_client_orders_from_notion(data['client'])
    keyboard = [[InlineKeyboardButton("📊 Excel Инвойс", callback_data='gen_excel')], [InlineKeyboardButton("📑 Export Airtable", callback_data='export_airtable')]]
    
    if client_orders:
        orders[uid]['existing_notion_page_id'] = client_orders[0]['id']
        msg_admin += f"\n\n⚠️ <b>Клиент найден в базе!</b> (Заказ от: {client_orders[0].get('date')})"
        keyboard.append([InlineKeyboardButton("🔄 Обновить старый", callback_data='paste_update'), InlineKeyboardButton("➕ Создать НОВЫЙ", callback_data='paste_new')])
    else:
        keyboard.append([InlineKeyboardButton("💾 Сохранить в Notion", callback_data='paste_new')])

    await update.message.reply_text(msg_admin, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))

async def export_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); uid = str(update.effective_user.id)
    if query.data == 'gen_excel':
        try:
            file_stream = await create_excel_invoice(uid)
            await context.bot.send_document(chat_id=query.message.chat_id, document=InputFile(file_stream, filename=f"Invoice_{orders[uid]['client']}.xlsx"))
        except: await query.message.reply_text("❌ Ошибка Excel.")
    elif query.data == 'export_airtable':
        data = orders.get(uid)
        export_text = f"AIRTABLE_EXPORT_START\nInvoice_ID: {get_code(data['client'])}\nDate: {datetime.now().strftime('%d.%m.%Y')}\nSum_Client_CNY: {data.get('total_cny_netto', 0)}\nReal_Purchase_CNY: {sum(i.get('purchase', 0) * i.get('qty', 0) for i in data.get('items', []))}\nClient_Rate: {data.get('client_rate', 58.0)}\nReal_Rate: {data.get('real_rate', 55.0)}\nTotal_Qty: {sum(i.get('qty', 0) for i in data.get('items', []))}\nChina_Logistics_CNY: {sum(i.get('delivery_factory', 0) for i in data.get('items', []))}\nFF_Boxes_Qty: {data.get('ff_boxes_qty', 0)}\nAIRTABLE_EXPORT_END"
        await query.message.reply_text(f"<code>{export_text}</code>", parse_mode='HTML')
    elif query.data in ['paste_new', 'paste_update', 'paste_save_direct']:
        if query.data == 'paste_update': orders[uid]['notion_page_id'] = orders[uid].get('existing_notion_page_id')
        elif query.data == 'paste_new': orders[uid]['notion_page_id'] = None
        url = await save_to_notion(uid)
        try: await query.edit_message_text(f"{query.message.text}\n\n✅ Сохранено:\n{url}" if url else f"{query.message.text}\n\n❌ Ошибка Notion")
        except: await query.message.reply_text(f"✅ Сохранено:\n{url}" if url else "❌ Ошибка Notion")

# ======== FF MENU И НАБОРЫ ========
F_MAIN_MENU, F_SINGLE_DIMS, F_BUNDLE_CREATE, F_BUNDLE_NAME, F_BUNDLE_DIMS, F_BUNDLE_PACKAGE, F_BUNDLE_THERMAL, F_BUNDLE_WORK, F_SUMMARY = range(20, 29)

async def cmd_ff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if uid not in orders or not orders[uid].get('items'): 
        await update.message.reply_text("Сначала /paste"); return ConversationHandler.END
    
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
        msg += f"{'☑️ <s>' if idx in items_in_bundles else '☐ '}{item['name']}{'</s>' if idx in items_in_bundles else ''}\n"
    
    msg += f"\n<b>Создано наборов:</b> {len(bundles)}\n"
    for b in bundles: msg += f"  📦 {b.get('name', 'Без имени')} (Кол-во: {b.get('qty', 1)})\n"
    
    keyboard = [
        [InlineKeyboardButton("📦 Собрать набор", callback_data='ff_mode_bundle')],
        [InlineKeyboardButton("✅ Завершить FF →", callback_data='ff_mode_continue')],
    ]
    if hasattr(update_or_query, 'edit_message_text'):
        await update_or_query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    else:
        await update_or_query.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    return F_MAIN_MENU

async def ff_main_menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); uid = str(update.effective_user.id)
    
    if query.data == 'ff_back_menu': return await show_ff_main_menu(query, uid)
        
    elif query.data == 'ff_mode_bundle':
        available = [(idx, item) for idx, item in enumerate(orders[uid]['items']) if idx not in orders[uid]['ff_items_in_bundles']]
        if not available: await query.answer("Нет товаров для набора", show_alert=True); return F_MAIN_MENU
        orders[uid]['ff_bundle_selected'] = set()
        orders[uid]['ff_bundle_available'] = available
        
        query_mock = type('obj', (object,), {'edit_message_text': query.edit_message_text})
        return await show_bundle_item_selection(query_mock, uid)
        
    elif query.data == 'ff_mode_continue':
        unpacked_indices = [idx for idx, item in enumerate(orders[uid]['items']) if idx not in orders[uid]['ff_items_in_bundles']]
        missing = [orders[uid]['items'][idx]['name'] for idx in unpacked_indices if orders[uid]['items'][idx].get('dims', (0,0,0)) == (0,0,0)]
        
        if missing:
            await query.edit_message_text(f"⚠️ У товара <b>{missing[0]}</b> нет размеров! Введи размеры (Д Ш В Вес):", parse_mode='HTML')
            orders[uid]['ff_missing_idx'] = unpacked_indices[0]
            return F_SINGLE_DIMS
            
        await query.edit_message_text("Напиши стоимость сборки/работы для ВСЕХ оставшихся одиночных товаров суммарно (¥) или 0:")
        return F_SUMMARY

async def ff_single_dims(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try:
        nums = tuple(map(float, update.message.text.replace(',', '.').split()))
        idx = orders[uid]['ff_missing_idx']
        if len(nums) >= 3:
            orders[uid]['items'][idx]['dims'] = (nums[0], nums[1], nums[2])
            if len(nums) >= 4: orders[uid]['items'][idx]['weight'] = nums[3]
        query_mock = type('obj', (object,), {'message': update.message, 'reply_text': update.message.reply_text})
        return await show_ff_main_menu(query_mock, uid)
    except:
        await update.message.reply_text("❌ Введи 4 числа (Д Ш В Вес):")
        return F_SINGLE_DIMS

async def show_bundle_item_selection(update_or_query, uid):
    available = orders[uid]['ff_bundle_available']
    selected = orders[uid]['ff_bundle_selected']
    keyboard = [[InlineKeyboardButton(f"{'☑️' if item_idx in selected else '☐'} {item['name']} x {item['qty']}", callback_data=f'ff_b_sel_{item_idx}')] for idx, (item_idx, item) in enumerate(available)]
    keyboard.append([InlineKeyboardButton("✅ Далее", callback_data='ff_b_next')])
    await update_or_query.edit_message_text("Выбери товары для набора:", reply_markup=InlineKeyboardMarkup(keyboard))
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
    await update.message.reply_text("Размеры ОДНОГО готового набора (Д Ш В Вес):"); return F_BUNDLE_DIMS

async def ff_bundle_dims(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try:
        nums = tuple(map(float, update.message.text.replace(',', '.').split()))
        orders[uid]['ff_b_dims'] = (nums[0], nums[1], nums[2])
        orders[uid]['ff_b_weight'] = nums[3] if len(nums) >= 4 else 0.0
    except:
        await update.message.reply_text("❌ Ошибка. Введи 4 числа (например: 16 12 5 0.3):")
        return F_BUNDLE_DIMS
        
    selected_indices = orders[uid].get('ff_bundle_selected', set())
    bundle_qty = min(orders[uid]['items'][i].get('qty', 1) for i in selected_indices) if selected_indices else 1
    orders[uid]['ff_b_qty'] = bundle_qty
    
    await update.message.reply_text(f"🤖 Бот рассчитал: получается <b>{bundle_qty} наборов</b>.", parse_mode='HTML')
    
    packages = await get_packages_from_notion()
    orders[uid]['ff_available_packages'] = packages
    keyboard = [[InlineKeyboardButton(f"📦 {p['name']} — {p['price']}¥", callback_data=f'ff_b_pkg_{i}')] for i, p in enumerate(packages)]
    keyboard.append([InlineKeyboardButton("💰 Своя цена", callback_data='ff_b_custom')])
    await update.message.reply_text("Выбери пакет:", reply_markup=InlineKeyboardMarkup(keyboard))
    return F_BUNDLE_PACKAGE

async def ff_bundle_package_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); uid = str(update.effective_user.id)
    if query.data == 'ff_b_custom':
        await query.edit_message_text("Цена пакета (¥):"); return F_BUNDLE_PACKAGE
    pkg_idx = int(query.data.replace('ff_b_pkg_', ''))
    orders[uid]['ff_b_pkg'] = orders[uid]['ff_available_packages'][pkg_idx]
    await query.edit_message_text("Цена сборки ЗА 1 НАБОР (¥):"); return F_BUNDLE_WORK

async def ff_bundle_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try: orders[uid]['ff_b_pkg'] = {'name': 'Ручной', 'price': float(update.message.text.replace(',', '.'))}
    except: await update.message.reply_text("Введи число:"); return F_BUNDLE_PACKAGE
    await update.message.reply_text("Цена сборки ЗА 1 НАБОР (¥):"); return F_BUNDLE_WORK

async def ff_bundle_work(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try: work = float(update.message.text.replace(',', '.'))
    except: await update.message.reply_text("Введи число:"); return F_BUNDLE_WORK
    
    orders[uid]['ff_bundles'].append({
        'name': orders[uid]['ff_b_name'], 'dims': orders[uid]['ff_b_dims'], 'weight': orders[uid].get('ff_b_weight', 0),
        'qty': orders[uid]['ff_b_qty'], 'pkg': orders[uid]['ff_b_pkg'], 'work_price': work,
        'item_indices': list(orders[uid]['ff_bundle_selected'])
    })
    orders[uid]['ff_items_in_bundles'].update(orders[uid]['ff_bundle_selected'])
    
    query_mock = type('obj', (object,), {'edit_message_text': update.message.reply_text})
    return await show_ff_main_menu(query_mock, uid)

async def ff_summary_work(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try: single_work = float(update.message.text.replace(',', '.'))
    except: await update.message.reply_text("Введи число:"); return F_SUMMARY
    
    # Считаем коробки (одиночные + наборы)
    unpacked_items = [item for idx, item in enumerate(orders[uid]['items']) if idx not in orders[uid]['ff_items_in_bundles']]
    bundle_items = [{'name': b['name'], 'dims': b['dims'], 'weight': b['weight'], 'qty': b['qty']} for b in orders[uid]['ff_bundles']]
    
    boxes = optimize_boxes_with_weight(unpacked_items + bundle_items)
    total_boxes = len(boxes)
    total_weight = sum(b['cur_weight'] for b in boxes)
    cost = total_boxes * BOX_PRICE_CNY
    
    orders[uid]['ff_total_yuan'] = cost + single_work + sum((b['pkg']['price'] + b['work_price']) * b['qty'] for b in orders[uid]['ff_bundles'])
    orders[uid]['ff_boxes_qty'] = total_boxes
    
    res = f"📦 <b>Результат FF (Лимит 30кг):</b>\n\nМест: {total_boxes} шт\nОбщий вес: {total_weight:.2f} кг\nСтоимость коробок: {cost:.2f}¥ (по {BOX_PRICE_CNY}¥)\nОбщий итог FF: {orders[uid]['ff_total_yuan']:.2f}¥"
    
    kb = [[InlineKeyboardButton("📊 Excel Инвойс", callback_data='gen_excel')], [InlineKeyboardButton("📑 Export Airtable", callback_data='export_airtable')], [InlineKeyboardButton("💾 Обновить Notion", callback_data='paste_save_direct')]]
    await update.message.reply_text(res, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb))
    return ConversationHandler.END

# ======== MAIN ========
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler('paste', cmd_paste))
    
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler('ff', cmd_ff)],
        states={
            F_MAIN_MENU: [CallbackQueryHandler(ff_main_menu_cb)],
            F_SINGLE_DIMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, ff_single_dims)],
            F_BUNDLE_CREATE: [CallbackQueryHandler(ff_bundle_cb)],
            F_BUNDLE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ff_bundle_name)],
            F_BUNDLE_DIMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, ff_bundle_dims)],
            F_BUNDLE_PACKAGE: [CallbackQueryHandler(ff_bundle_package_cb), MessageHandler(filters.TEXT & ~filters.COMMAND, ff_bundle_price)],
            F_BUNDLE_WORK: [MessageHandler(filters.TEXT & ~filters.COMMAND, ff_bundle_work)],
            F_SUMMARY: [MessageHandler(filters.TEXT & ~filters.COMMAND, ff_summary_work)],
        },
        fallbacks=[CommandHandler('cancel', lambda u,c: ConversationHandler.END)]
    ))
    
    app.add_handler(CallbackQueryHandler(export_handler))
    app.run_polling()

if __name__ == '__main__': main()
