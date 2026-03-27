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
# TODO: ВСТАВЬ СЮДА ID НОВОЙ БАЗЫ NOTION ДЛЯ ЧЕРНОВИКОВ CARGO!
CARGO_NOTION_DATABASE_ID = os.getenv('CARGO_NOTION_DB_ID', "СЮДА_ID_БАЗЫ_CARGO") 

BOX_PRICE_CNY = 7.77
MAX_BOX_WEIGHT = 30.0

notion = Client(auth=NOTION_TOKEN) if NOTION_TOKEN else None
orders = {}
cargo_drafts = {} # Локальное хранилище черновиков Карго

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

def generate_cargo_id():
    import random
    return f"CARGO-{random.randint(100, 999)}"

# ======== ЕДИНЫЙ ЦЕНТР РАСЧЕТОВ (ДЛЯ ТОВАРОВ) ========
# ... (Здесь остается весь старый код для /paste, /calc, /zakaz, /ff, /dostavka без изменений - я его свернул для краткости ответа, но он весь на месте!)
# ВНИМАНИЕ: При копировании в Railway, убедись, что старые функции (finalize_order, create_excel_invoice, cmd_paste и т.д.) остались на месте! Я покажу именно блок /cargo.

# ======== ЛОГИКА /CARGO (НОВЫЙ МОДУЛЬ) ========

# Состояния для интерактива Карго
CG_PACK, CG_DIMS, CG_T_CARGO, CG_T_CLIENT, CG_R_CNY, CG_R_AMD = range(60, 66)

def parse_cargo_text(text):
    data = {'client': 'Unknown', 'label': 'Без метки', 'items': []}
    current_item = None
    for line in text.split('\n'):
        l = line.strip()
        if not l: continue
        ll = l.lower()
        if 'клиент:' in ll: data['client'] = normalize_client_name(l.split(':', 1)[1])
        elif 'метка:' in ll: data['label'] = l.split(':', 1)[1].strip()
        elif 'товар' in ll:
            if current_item: data['items'].append(current_item)
            current_item = {'name': 'Товар', 'pieces': 0, 'weight': 0.0, 'dims': (0,0,0), 'pack_type': None, 'pack_price': 0.0}
        elif 'название:' in ll and current_item is not None:
            current_item['name'] = l.split(':', 1)[1].strip().title()
    if current_item: data['items'].append(current_item)
    return data

async def cmd_cargo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    text = update.message.text.replace('/cargo', '').strip()
    
    if uid not in cargo_drafts: cargo_drafts[uid] = {}

    # Если просто написали /cargo без текста -> Показываем меню активных партий
    if not text:
        active_parties = cargo_drafts[uid]
        if not active_parties:
            await update.message.reply_text("📂 Активных партий Карго нет. Чтобы создать, напиши /cargo и список товаров.")
            return ConversationHandler.END
            
        msg = "📂 **Ваши активные партии в Китае:**\n\n"
        keyboard = []
        for cid, draft in active_parties.items():
            ready = sum(1 for i in draft['items'] if i['pieces'] > 0)
            total = len(draft['items'])
            status = "Готов к расчету" if ready == total else "Ждет данных"
            btn_text = f"📦 {draft['client']} ({ready}/{total}) - {status}"
            keyboard.append([InlineKeyboardButton(btn_text, callback_data=f'cg_open_{cid}')])
            
        await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))
        return ConversationHandler.END

    # Если написали /cargo с текстом -> Создаем новую партию
    data = parse_cargo_text(text)
    if not data['items']: return await update.message.reply_text("❌ Ошибка: товары не найдены. Используй шаблон.")
    
    cargo_id = generate_cargo_id()
    data['cargo_id'] = cargo_id
    cargo_drafts[uid][cargo_id] = data
    
    total = len(data['items'])
    msg = f"💾 **Партия {cargo_id} сохранена!**\n👤 Клиент: {data['client']}\n🏷 Метка: {data['label']}\n\n⚠️ В партии {total} товаров. Ожидаем габариты для {total}/{total} позиций.\nВы можете закрыть чат, данные в безопасности."
    await update.message.reply_text(msg, parse_mode='Markdown')
    return ConversationHandler.END

async def cg_open_draft(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); uid = str(update.effective_user.id)
    cid = query.data.replace('cg_open_', '')
    draft = cargo_drafts[uid].get(cid)
    if not draft: return await query.message.reply_text("❌ Партия не найдена.")
    
    # Сохраняем активный ID
    orders[uid] = orders.get(uid, {})
    orders[uid]['active_cargo_id'] = cid
    
    ready = sum(1 for i in draft['items'] if i['pieces'] > 0)
    total = len(draft['items'])
    
    if ready < total:
        missing_names = [i['name'] for i in draft['items'] if i['pieces'] == 0]
        msg = f"📦 **Партия {cid} ({draft['client']})**\n\nГотово к расчету: {ready}/{total}.\nОжидаем данные для:\n- " + "\n- ".join(missing_names)
        kb = [[InlineKeyboardButton("✍️ Дополнить данные", callback_data='cg_fill')]]
        await query.message.reply_text(msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))
    else:
        # Сводка
        t_weight = sum(i['weight'] for i in draft['items'])
        t_vol = sum(i['pieces'] * (i['dims'][0]*i['dims'][1]*i['dims'][2])/1000000 for i in draft['items'])
        t_pieces = sum(i['pieces'] for i in draft['items'])
        density = int(t_weight / t_vol) if t_vol > 0 else 0
        
        msg = f"📦 **СВОДКА ДЛЯ КАРГО ({draft['client']}):**\n• Общий вес: {t_weight} кг\n• Общий объем: {t_vol:.2f} м³\n• Мест всего: {t_pieces} шт\n• Плотность: {density} кг/м³\n\n*Отправь это менеджеру карго для тарифа.*"
        kb = [[InlineKeyboardButton("🧮 Ввести тарифы и рассчитать", callback_data='cg_calc')]]
        await query.message.reply_text(msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))

# ==== ЦИКЛ ЗАПОЛНЕНИЯ (✍️ Дополнить данные) ====
async def cg_fill_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); uid = str(update.effective_user.id)
    cid = orders[uid]['active_cargo_id']
    draft = cargo_drafts[uid][cid]
    
    missing_idx = -1
    for idx, item in enumerate(draft['items']):
        if item['pieces'] == 0:
            missing_idx = idx
            break
            
    if missing_idx == -1:
        await query.message.reply_text("✅ Все данные уже заполнены! Открой партию заново, чтобы рассчитать.")
        return ConversationHandler.END
        
    orders[uid]['cg_missing_idx'] = missing_idx
    item_name = draft['items'][missing_idx]['name']
    
    kb = [
        [InlineKeyboardButton("🟡 Мешок ($5)", callback_data='cg_pack_sack')],
        [InlineKeyboardButton("📦 Уголки ($6)", callback_data='cg_pack_corners')],
        [InlineKeyboardButton("🪵 Обрешетка ($8)", callback_data='cg_pack_wood')]
    ]
    await query.message.reply_text(f"Выбери тип упаковки для товара **{item_name}**:", parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))
    return CG_PACK

async def cg_pack_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); uid = str(update.effective_user.id)
    idx = orders[uid]['cg_missing_idx']
    cid = orders[uid]['active_cargo_id']
    
    if query.data == 'cg_pack_sack': 
        cargo_drafts[uid][cid]['items'][idx]['pack_type'] = 'Мешок'
        cargo_drafts[uid][cid]['items'][idx]['pack_price'] = 5.0
    elif query.data == 'cg_pack_corners':
        cargo_drafts[uid][cid]['items'][idx]['pack_type'] = 'Уголки'
        cargo_drafts[uid][cid]['items'][idx]['pack_price'] = 6.0
    elif query.data == 'cg_pack_wood':
        cargo_drafts[uid][cid]['items'][idx]['pack_type'] = 'Обрешетка'
        cargo_drafts[uid][cid]['items'][idx]['pack_price'] = 8.0
        
    item_name = cargo_drafts[uid][cid]['items'][idx]['name']
    await query.message.reply_text(f"Введи данные от фабрики для **{item_name}**:\n*(Кол-во мест, Вес 1 места, Д Ш В)*\nНапример: 1 12.5 80 50 60", parse_mode='Markdown')
    return CG_DIMS

async def cg_dims_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    cid = orders[uid]['active_cargo_id']
    idx = orders[uid]['cg_missing_idx']
    
    try:
        nums = tuple(map(float, update.message.text.replace(',', '.').split()))
        if len(nums) < 5: raise ValueError
        
        pieces, weight_per_piece, l, w, h = int(nums[0]), nums[1], nums[2], nums[3], nums[4]
        pack_type = cargo_drafts[uid][cid]['items'][idx]['pack_type']
        
        # МАГИЯ НАКИДЫВАНИЯ ВЕСА И КУБОВ
        if pack_type == 'Уголки':
            weight_per_piece += 1.0 # +1 кг на картон
        elif pack_type == 'Обрешетка':
            weight_per_piece += 10.0 # +10 кг на дерево
            l += 5; w += 5; h += 5   # +5 см к каждой стороне
            
        cargo_drafts[uid][cid]['items'][idx].update({
            'pieces': pieces, 'weight': pieces * weight_per_piece, 'dims': (l, w, h)
        })
    except:
        await update.message.reply_text("❌ Введи 5 чисел через пробел (Места Вес Д Ш В):")
        return CG_DIMS
        
    # Ищем следующий пустой
    missing_idx = -1
    for i, item in enumerate(cargo_drafts[uid][cid]['items']):
        if item['pieces'] == 0:
            missing_idx = i; break
            
    if missing_idx == -1:
        await update.message.reply_text("✅ Все габариты заполнены! Открой /cargo, чтобы получить сводку.")
        return ConversationHandler.END
    else:
        orders[uid]['cg_missing_idx'] = missing_idx
        item_name = cargo_drafts[uid][cid]['items'][missing_idx]['name']
        kb = [[InlineKeyboardButton("🟡 Мешок ($5)", callback_data='cg_pack_sack')], [InlineKeyboardButton("📦 Уголки ($6)", callback_data='cg_pack_corners')], [InlineKeyboardButton("🪵 Обрешетка ($8)", callback_data='cg_pack_wood')]]
        await update.message.reply_text(f"Выбери тип упаковки для товара **{item_name}**:", parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))
        return CG_PACK

# ==== ЦИКЛ РАСЧЕТА (🧮 Ввести тарифы) ====
async def cg_calc_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    await query.message.reply_text("1. Введи тариф Карго (Себестоимость $/кг):")
    return CG_T_CARGO

async def cg_t_cargo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try: orders[uid]['cg_tc'] = float(update.message.text.replace(',', '.')); await update.message.reply_text("2. Введи тариф для Клиента ($/кг):"); return CG_T_CLIENT
    except: await update.message.reply_text("❌ Введи число:")
async def cg_t_client(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try: orders[uid]['cg_tcl'] = float(update.message.text.replace(',', '.')); await update.message.reply_text("3. Введи курс Карго (USD → CNY), по которому ты им платишь:"); return CG_R_CNY
    except: await update.message.reply_text("❌ Введи число:")
async def cg_r_cny(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try: orders[uid]['cg_rcny'] = float(update.message.text.replace(',', '.')); await update.message.reply_text("4. Введи курс Драма (CNY → AMD) для клиента:"); return CG_R_AMD
    except: await update.message.reply_text("❌ Введи число:")

async def cg_r_amd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try: orders[uid]['cg_ramd'] = float(update.message.text.replace(',', '.'))
    except: await update.message.reply_text("❌ Введи число:"); return CG_R_AMD
    
    cid = orders[uid]['active_cargo_id']
    draft = cargo_drafts[uid][cid]
    
    # Расчеты
    t_weight = sum(i['weight'] for i in draft['items'])
    t_vol = sum(i['pieces'] * (i['dims'][0]*i['dims'][1]*i['dims'][2])/1000000 for i in draft['items'])
    t_pieces = sum(i['pieces'] for i in draft['items'])
    
    pack_cost = sum(i['pieces'] * i['pack_price'] for i in draft['items'])
    unload_cost = t_pieces * 4.0 # $4 за выгрузку каждого места
    
    client_weight_usd = t_weight * orders[uid]['cg_tcl']
    client_total_usd = client_weight_usd + pack_cost + unload_cost
    client_total_amd = int(client_total_usd * orders[uid]['cg_rcny'] * orders[uid]['cg_ramd'])
    
    cargo_weight_usd = t_weight * orders[uid]['cg_tc']
    cargo_total_usd = cargo_weight_usd + pack_cost + unload_cost
    cargo_total_cny = int(cargo_total_usd * orders[uid]['cg_rcny'])
    
    profit_amd = client_total_amd - int(cargo_total_cny * orders[uid]['cg_ramd'])
    
    # Сохраняем финальные цифры для Airtable
    draft.update({'t_weight': t_weight, 't_vol': t_vol, 't_pieces': t_pieces, 'density': int(t_weight/t_vol) if t_vol>0 else 0, 'tc': orders[uid]['cg_tc'], 'tcl': orders[uid]['cg_tcl'], 'rcny': orders[uid]['cg_rcny'], 'ramd': orders[uid]['cg_ramd'], 'client_amd': client_total_amd, 'cargo_cny': cargo_total_cny, 'profit_amd': profit_amd})
    
    # Чек Клиенту
    msg_client = f"""🚛 **CARGO INVOICE: {draft['client'].upper()}**
🏷 {draft['label']}

**ПАРАМЕТРЫ ГРУЗА:**
• Вес брутто: {t_weight} кг
• Объем: {t_vol:.2f} м³
• Количество мест: {t_pieces} шт

**РАСЧЕТ СТОИМОСТИ:**
• Доставка за вес ({t_weight} кг × ${orders[uid]['cg_tcl']}): ${client_weight_usd:.1f}
• Доп. упаковка и услуги: ${pack_cost + unload_cost:.1f}

💵 Итого логистика: ${client_total_usd:.1f}
🔄 Конвертация: ${client_total_usd:.1f} × {orders[uid]['cg_rcny']} ¥ × {orders[uid]['cg_ramd']} AMD
✅ **К ОПЛАТЕ: {client_total_amd:,} AMD**"""
    
    await update.message.reply_text(msg_client, parse_mode='Markdown')
    
    # Внутренний чек
    msg_admin = f"""💼 **ВНУТРЕННИЙ РАСЧЕТ ({cid}):**

**1. ОТДАЕМ В КАРГО:**
• Себестоимость (${orders[uid]['cg_tc']}/кг + Упаковка/Выгрузка): **${cargo_total_usd:.1f}**
🇨🇳 **Перевести Карго: {cargo_total_cny:,} ¥** *(по курсу {orders[uid]['cg_rcny']})*

**2. ДОХОДЫ И ПРИБЫЛЬ:**
• Берем с клиента: {int(client_total_amd/orders[uid]['cg_ramd']):,} ¥ ({client_total_amd:,} AMD)
• Отдаем Карго: {cargo_total_cny:,} ¥
💰 **ЧИСТАЯ ПРИБЫЛЬ: {int(profit_amd/orders[uid]['cg_ramd']):,} ¥ ({profit_amd:,} AMD)**"""
    
    kb = [[InlineKeyboardButton("📊 Export Excel", callback_data='cg_export_ex')], [InlineKeyboardButton("📑 Export Airtable", callback_data='cg_export_air')], [InlineKeyboardButton("🗑 Завершить и удалить", callback_data='cg_delete')]]
    await update.message.reply_text(msg_admin, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))
    return ConversationHandler.END

# Обработчик кнопок Карго Экспорта
async def cg_export_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); uid = str(update.effective_user.id)
    cid = orders[uid].get('active_cargo_id')
    if not cid or cid not in cargo_drafts[uid]: return
    draft = cargo_drafts[uid][cid]
    
    if query.data == 'cg_export_air':
        export_text = f"AIRTABLE_EXPORT_START\nParty_ID: {cid}\nDate: {datetime.now().strftime('%d.%m.%Y')}\nTotal_Weight_KG: {draft['t_weight']}\nTotal_Volume_CBM: {draft['t_vol']:.2f}\nTotal_Pieces: {draft['t_pieces']}\nDensity: {draft['density']}\nPackaging_Type: Сборная\nTariff_Cargo_USD: {draft['tc']}\nTariff_Client_USD: {draft['tcl']}\nRate_USD_CNY: {draft['rcny']}\nRate_USD_AMD: {draft['ramd']}\nTotal_Client_AMD: {draft['client_amd']}\nTotal_Cargo_CNY: {draft['cargo_cny']}\nNet_Profit_AMD: {draft['profit_amd']}\nAIRTABLE_EXPORT_END"
        await query.message.reply_text(f"```text\n{export_text}\n```", parse_mode='Markdown')
        
    elif query.data == 'cg_delete':
        del cargo_drafts[uid][cid]
        await query.edit_message_text(f"{query.message.text}\n\n✅ **Партия закрыта и удалена из черновиков.**", parse_mode='Markdown')

# ======== MAIN (Сборка всего бота) ========
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Сюда добавь все свои старые CommandHandler и ConversationHandler для /zakaz, /ff, /dostavka, /calc, /paste!
    # ...
    
    # Новый хендлер для /cargo
    app.add_handler(CommandHandler('cargo', cmd_cargo))
    app.add_handler(CallbackQueryHandler(cg_open_draft, pattern='^cg_open_'))
    
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(cg_fill_start, pattern='^cg_fill$')],
        states={
            CG_PACK: [CallbackQueryHandler(cg_pack_cb, pattern='^cg_pack_')],
            CG_DIMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, cg_dims_input)]
        },
        fallbacks=[CommandHandler('cancel', lambda u,c: ConversationHandler.END)]
    ))
    
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(cg_calc_start, pattern='^cg_calc$')],
        states={
            CG_T_CARGO: [MessageHandler(filters.TEXT & ~filters.COMMAND, cg_t_cargo)],
            CG_T_CLIENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, cg_t_client)],
            CG_R_CNY: [MessageHandler(filters.TEXT & ~filters.COMMAND, cg_r_cny)],
            CG_R_AMD: [MessageHandler(filters.TEXT & ~filters.COMMAND, cg_r_amd)]
        },
        fallbacks=[CommandHandler('cancel', lambda u,c: ConversationHandler.END)]
    ))
    
    app.add_handler(CallbackQueryHandler(cg_export_handler, pattern='^cg_export_|^cg_delete$'))
    
    logger.info("Бот запущен. Версия v60 (Cargo Module & DBs)")
    app.run_polling()

if __name__ == '__main__': main()
