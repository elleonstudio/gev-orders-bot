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
MAX_BOX_WEIGHT = 30.0 # Лимит веса на одну коробку 60х40х40

notion = Client(auth=NOTION_TOKEN) if NOTION_TOKEN else None
orders = {}

TARIFFS = {
    'Коледино': 350, 'Невинномысск': 1100, 'Электросталь': 400, 'Белые Столбы': 350,
    'Чашниково': 350, 'Санкт-Петербург': 450, 'Казань': 450, 'Екатеринбург': 700,
    'Новосибирск': 850, 'Владивосток': 1000, 'Краснодар': 550, 'Свой тариф': 0
}

# ======== УТИЛИТЫ ========
def normalize_client_name(name):
    return re.sub(r'\s+', '', name).capitalize()

def get_code(client):
    return f"{client.upper()}-{datetime.now().strftime('%y%m%d')}"

def optimize_boxes_with_weight(items):
    """Алгоритм 3D + Вес (лимит 30кг)"""
    MAX_L, MAX_W, MAX_H = 60, 40, 40
    boxes = []
    
    all_units = []
    for item in items:
        for _ in range(item.get('qty', 0)):
            all_units.append({
                'name': item['name'],
                'dims': item.get('dims', (0,0,0)),
                'weight': item.get('weight', 0.0),
                'vol': item.get('dims', (1,1,1))[0] * item.get('dims', (1,1,1))[1] * item.get('dims', (1,1,1))[2]
            })

    for unit in all_units:
        placed = False
        unit_vol = unit['vol']
        unit_weight = unit['weight']
        
        for box in boxes:
            # Проверка объема и веса
            if box['rem_vol'] >= unit_vol and (box['cur_weight'] + unit_weight) <= MAX_BOX_WEIGHT:
                box['items'].append(unit)
                box['rem_vol'] -= unit_vol
                box['cur_weight'] += unit_weight
                placed = True
                break
        
        if not placed:
            boxes.append({
                'items': [unit],
                'rem_vol': (MAX_L * MAX_W * MAX_H) - unit_vol,
                'cur_weight': unit_weight
            })
    return boxes

# ======== EXCEL ГЕНЕРАТОР ========
async def create_excel_invoice(uid):
    data = orders[uid]
    items_data = []
    for i, item in enumerate(data['items'], 1):
        delivery = item.get('delivery_factory', 0)
        total_item_cny = (item['price'] * item['qty']) + delivery
        items_data.append({
            "№": i,
            "Название товара": item['name'],
            "Кол-во (шт)": item['qty'],
            "Цена (¥)": item['price'],
            "Логистика (¥)": delivery,
            "Итого (¥)": total_item_cny
        })
    
    df = pd.DataFrame(items_data)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Invoice')
        # Доп инфо можно дописать ниже через openpyxl если нужно
        
    output.seek(0)
    return output

# ======== NOTION SAVE ========
async def save_to_notion(uid):
    if not notion: return None
    try:
        data = orders[uid]
        items = data['items']
        total_qty = sum(i['qty'] for i in items)
        total_cny = data['total_cny_netto']
        total_amd = data['final_total_amd']
        
        properties = {
            "Код заказа": {"title": [{"text": {"content": get_code(data['client'])}}]},
            "Клиент": {"select": {"name": data['client']}},
            "Количество": {"number": float(total_qty)},
            " К ОПЛАТЕ (AMD)": {"number": float(total_amd)},
            "Статус": {"select": {"name": "Новый"}},
            "Date": {"date": {"start": datetime.now().strftime('%Y-%m-%d')}}
        }
        
        page_id = data.get('notion_page_id')
        if page_id: res = notion.pages.update(page_id=page_id, properties=properties)
        else: res = notion.pages.create(parent={"database_id": NOTION_DATABASE_ID}, properties=properties)
        return f"https://notion.so/{res['id'].replace('-', '')}"
    except: return None

# ======== COMMANDS ========
async def cmd_paste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    text = update.message.text.replace('/paste', '').strip()
    if not text: return
    
    lines = text.strip().split('\n')
    client = 'Unknown'; items = []; client_rate = 58.0; real_rate = 55.0; current_item = None
    
    for line in lines:
        l = line.strip().lower()
        if 'клиент:' in l: client = normalize_client_name(line.split(':', 1)[1])
        elif 'товар' in l:
            if current_item: items.append(current_item)
            current_item = {'dims': (0,0,0), 'weight': 0.0}
        elif 'название:' in l: current_item['name'] = line.split(':', 1)[1].strip()
        elif 'количество:' in l: current_item['qty'] = int(re.search(r'\d+', l).group())
        elif 'цена клиенту:' in l: current_item['price'] = float(line.split(':', 1)[1].replace(',','.').strip())
        elif 'закупка:' in l: current_item['purchase'] = float(line.split(':', 1)[1].replace(',','.').strip())
        elif 'доставка:' in l: current_item['delivery_factory'] = float(line.split(':', 1)[1].replace(',','.').strip())
        elif 'размеры:' in l:
            parts = line.split(':', 1)[1].replace(',','.').split()
            if len(parts) >= 3: current_item['dims'] = tuple(map(float, parts[:3]))
            if len(parts) >= 4: current_item['weight'] = float(parts[3])
        elif 'курс клиенту:' in l: client_rate = float(line.split(':', 1)[1].replace(',','.').strip())
        elif 'мой курс:' in l: real_rate = float(line.split(':', 1)[1].replace(',','.').strip())

    if current_item: items.append(current_item)
    
    # Математика
    subtotal_cny = sum((i['price'] * i['qty']) + i.get('delivery_factory', 0) for i in items)
    comm_cny_3pct = subtotal_cny * 0.03
    comm_amd_3pct = comm_cny_3pct * client_rate
    
    rule_applied = False
    if comm_amd_3pct < 10000:
        actual_comm_amd = 10000
        actual_comm_cny = 10000 / client_rate
        rule_applied = True
    else:
        actual_comm_amd = int(comm_amd_3pct)
        actual_comm_cny = comm_cny_3pct

    final_total_amd = int((subtotal_cny * client_rate) + actual_comm_amd)
    
    orders[uid] = {
        'client': client, 'items': items, 'client_rate': client_rate, 
        'real_rate': real_rate, 'final_total_amd': final_total_amd,
        'total_cny_netto': subtotal_cny
    }

    # АУДИТ
    audit = []
    if rule_applied: audit.append(f"• Применено правило 10 000 AMD (3% было {int(comm_amd_3pct)} AMD)")
    missing = [i['name'] for i in items if i['dims'] == (0,0,0)]
    if missing: audit.append(f"• У товара <b>{', '.join(missing)}</b> нет размеров. Спрошу в /ff.")
    
    if audit: await update.message.reply_text("⚠️ <b>Аудит расчета:</b>\n" + "\n".join(audit), parse_mode='HTML')

    # ИНВОЙС
    inv_lines = ""
    for i in items:
        d = i.get('delivery_factory', 0)
        inv_lines += f"• {i['name']} — {i['qty']} шт\n{i['qty']} × {i['price']} + {d} = {(i['price']*i['qty'])+d:.1f}¥\n"
    
    fee_label = "Минимальная 10000 AMD" if rule_applied else "3%"
    
    msg = f"""<b>COMMERCIAL INVOICE: {client.upper()}</b>
📅 Date: {datetime.now().strftime('%d.%m.%Y')}

<b>1. ТОВАРНАЯ ВЕДОМОСТЬ (Logistics Included)</b>
{inv_lines}<code>────────────────────────</code>
<b>SUBTOTAL:</b> {subtotal_cny:.1f}¥

<b>2. КОМИССИЯ И СЕРВИС (Service Fee)</b>
({fee_label}): {actual_comm_cny:.1f}¥

<b>3. ИТОГОВЫЙ РАСЧЕТ (Convertation)</b>
• Всего в юанях: {subtotal_cny + actual_comm_cny:.1f}¥
• Курс обмена (Exchange): {client_rate}

✅ <b>ИТОГО К ОПЛАТЕ: {final_total_amd:,} AMD</b>"""

    keyboard = [
        [InlineKeyboardButton("📊 Excel Инвойс", callback_data='gen_excel')],
        [InlineKeyboardButton("💾 Сохранить в Notion", callback_data='paste_save')]
    ]
    await update.message.reply_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); uid = str(update.effective_user.id)
    if query.data == 'gen_excel':
        file_stream = await create_excel_invoice(uid)
        await context.bot.send_document(chat_id=query.message.chat_id, document=InputFile(file_stream, filename=f"Invoice_{orders[uid]['client']}.xlsx"))
    elif query.data == 'paste_save':
        url = await save_to_notion(uid)
        await query.edit_message_text(f"✅ Сохранено в Notion:\n{url}" if url else "❌ Ошибка Notion")

# ======== FF LOGIC ========
F_MAIN, F_SINGLE, F_BUNDLE_NAME, F_BUNDLE_DIMS, F_WORK = range(20, 25)

async def cmd_ff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if uid not in orders: return
    orders[uid]['ff_boxes'] = []
    orders[uid]['ff_items_in_bundles'] = set()
    orders[uid]['ff_bundles'] = []
    
    await update.message.reply_text("📦 <b>FF Режим (Лимит 30кг)</b>\n7.77¥ за коробку.\n\n/ff_bundle - собрать набор\n/ff_done - завершить", parse_mode='HTML')

async def cmd_ff_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    data = orders[uid]
    
    # Собираем всё что не в наборах
    unpacked = []
    for idx, item in enumerate(data['items']):
        if idx not in data['ff_items_in_bundles']:
            if item['dims'] == (0,0,0):
                await update.message.reply_text(f"❌ У товара {item['name']} нет размеров! Используй /ff_single для него.")
                return
            unpacked.append(item)
    
    # Добавляем уже созданные наборы как готовые единицы
    for b in data['ff_bundles']:
        unpacked.append({'name': b['name'], 'dims': b['dims'], 'qty': b['qty'], 'weight': b['weight']})
    
    boxes = optimize_boxes_with_weight(unpacked)
    total_boxes = len(boxes)
    total_weight = sum(b['cur_weight'] for b in boxes)
    cost = total_boxes * BOX_PRICE_CNY
    
    data['ff_total_yuan'] = cost
    await save_to_notion(uid)
    
    res = f"📦 <b>Результат FF:</b>\nМест: {total_boxes} шт\nОбщий вес: {total_weight:.2f} кг\nСтоимость коробок: {cost:.2f}¥\n\nДанные веса переданы в /dostavka"
    await update.message.reply_text(res, parse_mode='HTML')

# ======== MAIN ========
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('paste', cmd_paste))
    app.add_handler(CommandHandler('ff', cmd_ff))
    app.add_handler(CommandHandler('ff_done', cmd_ff_done))
    app.add_handler(CallbackQueryHandler(callback_handler))
    
    # Добавь сюда ConversationHandlers для zakaz и dostavka из прошлых версий
    # с учетом normalize_client_name и optimize_boxes_with_weight
    
    app.run_polling()

if __name__ == '__main__': main()