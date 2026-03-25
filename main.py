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

# ======== УТИЛИТЫ ========
def normalize_client_name(name):
    return re.sub(r'\s+', '', name).strip().capitalize()

def get_code(client):
    return f"{client.upper()}-{datetime.now().strftime('%y%m%d')}"

def optimize_boxes_with_weight(items):
    """Алгоритм упаковки 3D + Вес (лимит 30кг)"""
    MAX_L, MAX_W, MAX_H = 60, 40, 40
    boxes = []
    
    # Разворачиваем товары в единичный список для поштучной укладки
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
            # Проверяем и объем, и вес
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

# ======== EXCEL ГЕНЕРАТОР (xlsxwriter) ========
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
    # Используем xlsxwriter для надежности в Railway
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df.to_excel(writer, index=False, sheet_name='Invoice')
    output.seek(0)
    return output

# ======== NOTION SAVE ========
async def save_to_notion(uid):
    if not notion: return None
    try:
        data = orders[uid]
        client = data['client']
        total_qty = sum(i['qty'] for i in data['items'])
        
        properties = {
            "Код заказа": {"title": [{"text": {"content": get_code(client)}}]},
            "Клиент": {"select": {"name": client}},
            "Количество": {"number": float(total_qty)},
            " К ОПЛАТЕ (AMD)": {"number": float(data['final_total_amd'])},
            "Статус": {"select": {"name": "Новый"}},
            "Date": {"date": {"start": datetime.now().strftime('%Y-%m-%d')}}
        }
        
        # Если это FF, добавляем стоимость коробок
        if 'ff_total_yuan' in data:
            properties["ИТОГО (CNY)"] = {"number": float(data['ff_total_yuan'])}

        res = notion.pages.create(parent={"database_id": NOTION_DATABASE_ID}, properties=properties)
        return f"https://notion.so/{res['id'].replace('-', '')}"
    except Exception as e:
        logger.error(f"Notion Error: {e}")
        return None

# ======== ПАРСЕР /PASTE ========
def parse_paste_text(text):
    data = {'client': 'Unknown', 'items': [], 'client_rate': 58.0, 'real_rate': 55.0}
    current_item = None
    lines = text.split('\n')
    
    for line in lines:
        l = line.strip().lower()
        if not l: continue
        
        if 'клиент:' in l:
            data['client'] = normalize_client_name(line.split(':', 1)[1])
        elif 'товар' in l:
            if current_item: data['items'].append(current_item)
            current_item = {'name': 'Товар', 'qty': 0, 'price': 0.0, 'delivery_factory': 0.0, 'dims': (0,0,0), 'weight': 0.0}
        elif 'название:' in l:
            current_item['name'] = line.split(':', 1)[1].strip().title()
        elif 'количество:' in l:
            nums = re.findall(r'\d+', l)
            if nums: current_item['qty'] = int(nums[0])
        elif 'цена клиенту:' in l:
            nums = re.findall(r'\d+\.?\d*', l.replace(',', '.'))
            if nums: current_item['price'] = float(nums[0])
        elif 'доставка:' in l:
            nums = re.findall(r'\d+\.?\d*', l.replace(',', '.'))
            if nums: current_item['delivery_factory'] = float(nums[0])
        elif 'размеры:' in l:
            nums = re.findall(r'\d+\.?\d*', l.replace(',', '.'))
            if len(nums) >= 3:
                current_item['dims'] = (float(nums[0]), float(nums[1]), float(nums[2]))
                if len(nums) >= 4: current_item['weight'] = float(nums[3])
        elif 'курс клиенту:' in l:
            nums = re.findall(r'\d+\.?\d*', l.replace(',', '.'))
            if nums: data['client_rate'] = float(nums[0])
        elif 'мой курс:' in l:
            nums = re.findall(r'\d+\.?\d*', l.replace(',', '.'))
            if nums: data['real_rate'] = float(nums[0])

    if current_item: data['items'].append(current_item)
    return data

# ======== COMMANDS ========
async def cmd_paste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    text = update.message.text.replace('/paste', '').strip()
    if not text: return

    data = parse_paste_text(text)
    subtotal_cny = sum((i['price'] * i['qty']) + i['delivery_factory'] for i in data['items'])
    comm_amd_3pct = (subtotal_cny * 0.03) * data['client_rate']
    
    rule_applied = False
    if comm_amd_3pct < 10000:
        actual_comm_amd = 10000
        actual_comm_cny = 10000 / data['client_rate']
        rule_applied = True
    else:
        actual_comm_amd = int(comm_amd_3pct)
        actual_comm_cny = subtotal_cny * 0.03

    final_total_amd = int((subtotal_cny * data['client_rate']) + actual_comm_amd)
    
    orders[uid] = data
    orders[uid].update({'final_total_amd': final_total_amd, 'total_cny_netto': subtotal_cny, 'rule_applied': rule_applied, 'actual_comm_cny': actual_comm_cny})

    # Аудит
    audit = []
    if rule_applied: audit.append(f"• Применено правило 10 000 AMD (3% было {int(comm_amd_3pct)} AMD)")
    missing = [i['name'] for i in data['items'] if i['dims'] == (0,0,0)]
    if missing: audit.append(f"• У <b>{', '.join(missing)}</b> нет размеров. Спрошу в /ff.")
    if audit: await update.message.reply_text("⚠️ <b>Аудит расчета:</b>\n" + "\n".join(audit), parse_mode='HTML')

    # Чек
    inv_lines = ""
    for i in data['items']:
        total_line = (i['price'] * i['qty']) + i['delivery_factory']
        inv_lines += f"• {i['name']} — {i['qty']} шт\n{i['qty']} × {i['price']} + {i['delivery_factory']} = {total_line:.1f}¥\n"

    msg = f"<b>COMMERCIAL INVOICE: {data['client'].upper()}</b>\n📅 Date: {datetime.now().strftime('%d.%m.%Y')}\n\n"
    msg += f"<b>1. ТОВАРНАЯ ВЕДОМОСТЬ (Logistics Included)</b>\n{inv_lines}<code>────────────────────────</code>\n<b>SUBTOTAL:</b> {subtotal_cny:.1f}¥\n\n"
    msg += f"<b>2. КОМИССИЯ И СЕРВИС (Service Fee)</b>\n({'Минимальная 10000 AMD' if rule_applied else '3%'}): {actual_comm_cny:.1f}¥\n\n"
    msg += f"<b>3. ИТОГОВЫЙ РАСЧЕТ (Convertation)</b>\n• Всего в юанях: {subtotal_cny + actual_comm_cny:.1f}¥\n• Курс: {data['client_rate']}\n\n✅ <b>ИТОГО: {final_total_amd:,} AMD</b>"

    kb = [[InlineKeyboardButton("📊 Excel Инвойс", callback_data='gen_excel')], [InlineKeyboardButton("💾 Сохранить в Notion", callback_data='paste_save')]]
    await update.message.reply_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb))

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); uid = str(update.effective_user.id)
    if query.data == 'gen_excel':
        try:
            file_stream = await create_excel_invoice(uid)
            await context.bot.send_document(chat_id=query.message.chat_id, document=InputFile(file_stream, filename=f"Invoice_{orders[uid]['client']}.xlsx"))
        except: await query.message.reply_text("❌ Ошибка Excel. Проверь требования.")
    elif query.data == 'paste_save':
        url = await save_to_notion(uid)
        await query.message.reply_text(f"✅ Сохранено:\n{url}" if url else "❌ Ошибка Notion")

# ======== ФУНКЦИИ FF ========
async def cmd_ff_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if uid not in orders: return
    
    items_to_pack = orders[uid]['items']
    boxes = optimize_boxes_with_weight(items_to_pack)
    total_boxes = len(boxes)
    total_weight = sum(b['cur_weight'] for b in boxes)
    cost = total_boxes * BOX_PRICE_CNY
    
    orders[uid]['ff_total_yuan'] = cost
    await save_to_notion(uid)
    
    res = f"📦 <b>Результат FF (Лимит 30кг):</b>\n\nМест: {total_boxes} шт\nОбщий вес: {total_weight:.2f} кг\nСтоимость коробок: {cost:.2f}¥ (по {BOX_PRICE_CNY}¥)"
    await update.message.reply_text(res, parse_mode='HTML')

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler('paste', cmd_paste))
    app.add_handler(CommandHandler('ff_done', cmd_ff_done))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.run_polling()

if __name__ == '__main__': main()