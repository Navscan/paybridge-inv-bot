import logging
import asyncio
import re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes
)
import bcrypt
from bson.objectid import ObjectId
from database import (
    get_shop, create_shop,
    save_order_structured,
    get_order, get_most_recent_order,
    get_pin_hash
)
from fpdf import FPDF
import tempfile
import os
from datetime import datetime

# Enable logging
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                    level=logging.INFO)
logger = logging.getLogger(__name__)

# Conversation states
SELECTING_SIZE, SELECTING_QUANTITY, CONFIRM_ADD_MORE, CHOOSE_SAVED_OR_NEW, ASK_NAME, ASK_PHONE, ASK_ADDRESS, ASK_PIN = range(8)
DELIVERY_WAITING_PIN = 8  # new state for /mydelivery

# Product details
PRODUCT_NAME = "Café Gold Ultrarum"
PRICE_PER_LITER = 170.00  # R

SIZES = {
    "0.75": "750ml",
    "1.0": "1L",
    "5.0": "5L",
}

ADMIN_CHAT_ID = 5548371987
DISPATCH_CHAT_ID = 5548371987  # change to your courier group ID

def main_menu_keyboard():
    keyboard = [[KeyboardButton("🛒 Place Order")]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)

def size_keyboard():
    keyboard = []
    for value, label in SIZES.items():
        keyboard.append([InlineKeyboardButton(label, callback_data=value)])
    return InlineKeyboardMarkup(keyboard)

# /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Welcome to Brand Delicious!\n"
        "We offer Café Gold Ultrarum – premium flavour syrup.\n"
        "Tap the 'Place Order' button below to start.",
        reply_markup=main_menu_keyboard()
    )

# ---------- Order Placement ----------
async def new_order_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Please select a size:",
        reply_markup=size_keyboard()
    )
    context.user_data['order_items'] = []
    return SELECTING_SIZE

async def size_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    size_liters = float(query.data)
    context.user_data['current_size'] = size_liters
    size_label = SIZES[query.data]
    await query.edit_message_text(
        f"Selected size: {size_label}\n"
        "Enter quantity (e.g., 5):"
    )
    return SELECTING_QUANTITY

async def quantity_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        qty = int(update.message.text.strip())
        if qty <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Please enter a positive number.")
        return SELECTING_QUANTITY

    size_liters = context.user_data['current_size']
    size_label = SIZES[str(size_liters)]
    context.user_data['order_items'].append((size_liters, qty))

    keyboard = [
        [InlineKeyboardButton("Yes, add another size", callback_data="add_more")],
        [InlineKeyboardButton("No, continue to customer details", callback_data="done")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        f"Added {qty} x {size_label} {PRODUCT_NAME}.\n"
        "Do you want to add another size?",
        reply_markup=reply_markup
    )
    return CONFIRM_ADD_MORE

async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "add_more":
        await query.edit_message_text(
            "Select another size:",
            reply_markup=size_keyboard()
        )
        return SELECTING_SIZE
    else:
        user_id = update.effective_user.id
        shop = get_shop(user_id)
        if shop:
            keyboard = [
                [InlineKeyboardButton("✅ Use saved details", callback_data="use_saved")],
                [InlineKeyboardButton("✏️ Enter new details", callback_data="enter_new")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                "We have your previous details:\n"
                f"*Name:* {shop['name']}\n"
                f"*Phone:* {shop['phone']}\n"
                f"*Address:* {shop['address']}\n\n"
                "Do you want to use these or enter new ones?",
                reply_markup=reply_markup,
                parse_mode="Markdown"
            )
            return CHOOSE_SAVED_OR_NEW
        else:
            await query.edit_message_text("Please enter your name or shop name:")
            return ASK_NAME

async def choose_saved_or_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "use_saved":
        user_id = update.effective_user.id
        shop = get_shop(user_id)
        context.user_data['customer_name'] = shop['name']
        context.user_data['customer_phone'] = shop['phone']
        context.user_data['customer_address'] = shop['address']
        await query.edit_message_text("Please enter your shop PIN:")
        return ASK_PIN
    else:
        await query.edit_message_text("Please enter your name or shop name:")
        return ASK_NAME

async def ask_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['customer_name'] = update.message.text.strip()
    await update.message.reply_text("Thank you. Please enter your phone number (e.g., 0812345678):")
    return ASK_PHONE

async def ask_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['customer_phone'] = update.message.text.strip()
    await update.message.reply_text("Now enter your delivery address (street, city, etc.):")
    return ASK_ADDRESS

async def ask_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['customer_address'] = update.message.text.strip()
    await update.message.reply_text("Please set a 4‑digit PIN for your shop (will be used for all future orders):")
    return ASK_PIN

async def receive_pin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pin = update.message.text.strip()
    if not pin.isdigit() or len(pin) != 4:
        await update.message.reply_text("Invalid PIN. Please enter exactly 4 digits.")
        return ASK_PIN

    user_id = update.effective_user.id
    customer_name = context.user_data.get('customer_name', '')
    customer_phone = context.user_data.get('customer_phone', '')
    customer_address = context.user_data.get('customer_address', '')

    shop = get_shop(user_id)
    if shop:
        if not bcrypt.checkpw(pin.encode('utf-8'), shop['pin_hash']):
            await update.message.reply_text("❌ Incorrect PIN. Please try again.")
            return ASK_PIN
    else:
        create_shop(user_id, customer_name, customer_phone, customer_address, pin)

    items_list = context.user_data['order_items']
    total = 0.0
    structured_items = []
    items_text_lines = []  # for dispatch
    for size_liters, qty in items_list:
        size_label = SIZES[str(size_liters)]
        unit_price = size_liters * PRICE_PER_LITER
        line_total = unit_price * qty
        total += line_total
        structured_items.append({
            "description": f"{size_label} {PRODUCT_NAME}",
            "quantity": qty,
            "unit_price": unit_price,
            "line_total": line_total
        })
        items_text_lines.append(f"{qty} x {size_label} {PRODUCT_NAME}")

    order_id = save_order_structured(
        user_id,
        structured_items,
        total,
        customer_name=customer_name,
        customer_phone=customer_phone,
        customer_address=customer_address
    )

    await update.message.reply_text(
        f"✅ Order placed! Your order ID is: `{order_id}`\n"
        f"Use /myinvoice and enter your shop PIN to get the invoice.\n"
        f"Use /mydelivery to get a delivery note (no prices).",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard()
    )

    # ----- TEXT‑ONLY DISPATCH NOTE -----
    try:
        dispatch_message = (
            f"📦 *New order ready for dispatch*\n"
            f"*Order ID:* `{order_id}`\n"
            f"*Customer:* {customer_name}\n"
            f"*Phone:* {customer_phone}\n"
            f"*Address:* {customer_address}\n"
            f"*Items:*\n" + "\n".join(items_text_lines)
        )
        await context.bot.send_message(
            chat_id=DISPATCH_CHAT_ID,
            text=dispatch_message,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Failed to send dispatch notification: {e}")

    # ----- Admin notification (with prices) -----
    items_text = "\n".join([f"{item['quantity']} x {item['description']} @ R{item['unit_price']:.2f} = R{item['line_total']:.2f}" for item in structured_items])
    admin_message = (
        f"🆕 *New Order Received!*\n"
        f"*Order ID:* `{order_id}`\n"
        f"*Customer:* {customer_name}\n"
        f"*Phone:* {customer_phone}\n"
        f"*Address:* {customer_address}\n"
        f"*Items:*\n{items_text}\n"
        f"*Total:* R{total:.2f}\n"
        f"*User Telegram ID:* {user_id}"
    )
    try:
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=admin_message, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Failed to send admin notification: {e}")

    context.user_data.clear()
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Order cancelled.",
        reply_markup=main_menu_keyboard()
    )
    context.user_data.clear()
    return ConversationHandler.END

# ---------- INVOICE (with prices, PDF) ----------
async def myinvoice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📄 *Get your most recent invoice*\n\n"
        "Please enter your 4‑digit shop PIN, or send `ORDER_ID PIN` for a specific order:",
        parse_mode="Markdown"
    )

async def handle_invoice_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_input = update.message.text.strip()
    user_id = update.effective_user.id

    if re.match(r'^\d{4}$', user_input):
        pin = user_input
        shop = get_shop(user_id)
        if not shop:
            await update.message.reply_text("No shop account found. If you have an older order, please use the format `ORDER_ID PIN`.")
            return
        if not bcrypt.checkpw(pin.encode('utf-8'), shop['pin_hash']):
            await update.message.reply_text("❌ Incorrect PIN.")
            return
        order = get_most_recent_order(user_id)
        if not order:
            await update.message.reply_text("You have no orders yet.")
            return
        await send_invoice(update, order)

    elif re.match(r'^[0-9a-fA-F]{24}\s+\d{4}$', user_input):
        order_id_str, pin = user_input.split()
        order = get_order(order_id_str)
        if not order:
            await update.message.reply_text("❌ Order not found.")
            return
        pin_hash = get_pin_hash(order_id_str)
        if pin_hash:
            if not bcrypt.checkpw(pin.encode('utf-8'), pin_hash):
                await update.message.reply_text("❌ Incorrect PIN.")
                return
        else:
            shop = get_shop(order['user_id'])
            if not shop or not bcrypt.checkpw(pin.encode('utf-8'), shop['pin_hash']):
                await update.message.reply_text("❌ Incorrect PIN.")
                return
        await send_invoice(update, order)

    else:
        await update.message.reply_text(
            "❌ Invalid format. Please send either a 4‑digit PIN (for most recent order) or `ORDER_ID PIN`."
        )

async def send_invoice(update: Update, order):
    customer_data = {
        'name': order.get('customer_name', 'N/A'),
        'phone': order.get('customer_phone', 'N/A'),
        'address': order.get('customer_address', 'N/A')
    }
    business_data = {
        'name': 'Paybridge (Pty) Ltd t/a Brand Delicious',
        'reg_no': '2016/150225/07',
        'vat_id': '9295481197',
        'address': 'The Old Biscuit Mill, 373a Albert Road, Woodstock, Cape Town',
        'phone': '+27685665931',
        'bank_details': 'Bank: FNB, Account: 123456789, Branch: 250655'
    }
    pdf_path = generate_invoice_pdf(str(order['_id']), order, customer_data, business_data)
    with open(pdf_path, 'rb') as pdf_file:
        await update.message.reply_document(
            document=pdf_file,
            filename=f"Invoice_{str(order['_id'])[-8:]}.pdf",
            caption="📄 Your VAT invoice is attached."
        )
    os.unlink(pdf_path)

# ---------- DELIVERY NOTE (PDF, no prices) ----------
async def mydelivery(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📦 *Get a delivery note* (no prices)\n\n"
        "Please enter your 4‑digit shop PIN for your most recent order,\n"
        "or send `ORDER_ID PIN` for a specific order:",
        parse_mode="Markdown"
    )
    return DELIVERY_WAITING_PIN

async def handle_delivery_pin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_input = update.message.text.strip()
    user_id = update.effective_user.id

    # Check if it's a 4-digit PIN (most recent order)
    if re.match(r'^\d{4}$', user_input):
        pin = user_input
        shop = get_shop(user_id)
        if not shop:
            await update.message.reply_text("No shop account found. If you have an older order, please use the format `ORDER_ID PIN`.")
            return ConversationHandler.END
        if not bcrypt.checkpw(pin.encode('utf-8'), shop['pin_hash']):
            await update.message.reply_text("❌ Incorrect PIN.")
            return ConversationHandler.END
        order = get_most_recent_order(user_id)
        if not order:
            await update.message.reply_text("You have no orders yet.")
            return ConversationHandler.END
        await send_delivery_pdf(update, order)

    # Check if it's "ORDER_ID PIN" format
    elif re.match(r'^[0-9a-fA-F]{24}\s+\d{4}$', user_input):
        order_id_str, pin = user_input.split()
        order = get_order(order_id_str)
        if not order:
            await update.message.reply_text("❌ Order not found.")
            return ConversationHandler.END
        # Verify PIN
        pin_hash = get_pin_hash(order_id_str)
        if pin_hash:
            if not bcrypt.checkpw(pin.encode('utf-8'), pin_hash):
                await update.message.reply_text("❌ Incorrect PIN.")
                return ConversationHandler.END
        else:
            shop = get_shop(order['user_id'])
            if not shop or not bcrypt.checkpw(pin.encode('utf-8'), shop['pin_hash']):
                await update.message.reply_text("❌ Incorrect PIN.")
                return ConversationHandler.END
        await send_delivery_pdf(update, order)

    else:
        await update.message.reply_text(
            "❌ Invalid format. Please send either a 4‑digit PIN (for most recent order) or `ORDER_ID PIN`."
        )
        return DELIVERY_WAITING_PIN

    return ConversationHandler.END

async def send_delivery_pdf(update: Update, order):
    """Generate and send a PDF delivery note (no prices)."""
    customer_data = {
        'name': order.get('customer_name', 'N/A'),
        'phone': order.get('customer_phone', 'N/A'),
        'address': order.get('customer_address', 'N/A')
    }
    business_data = {
        'name': 'Paybridge (Pty) Ltd t/a Brand Delicious',
        'address': 'The Old Biscuit Mill, 373a Albert Road, Woodstock, Cape Town',
        'phone': '+27685665931'
    }
    pdf_path = generate_delivery_note_pdf(str(order['_id']), order, customer_data, business_data)
    with open(pdf_path, 'rb') as pdf_file:
        await update.message.reply_document(
            document=pdf_file,
            filename=f"Delivery_{str(order['_id'])[-8:]}.pdf",
            caption="📦 Your delivery note is attached. No prices shown."
        )
    os.unlink(pdf_path)

async def cancel_delivery(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Delivery note request cancelled.", reply_markup=main_menu_keyboard())
    return ConversationHandler.END

# ---------- PDF GENERATION ----------
def generate_delivery_note_pdf(order_id, order_data, customer_data, business_data):
    """PDF with no prices, totals, or bank details."""
    pdf = FPDF()
    pdf.add_page()

    # Header
    pdf.set_font('Arial', 'B', 16)
    pdf.cell(0, 10, 'DELIVERY NOTE', 0, 1, 'C')
    pdf.ln(5)

    # Business details
    pdf.set_font('Arial', 'B', 12)
    pdf.cell(0, 8, business_data['name'], 0, 1)
    pdf.set_font('Arial', '', 10)
    pdf.cell(0, 6, business_data['address'], 0, 1)
    pdf.cell(0, 6, f"Tel: {business_data['phone']}", 0, 1)
    pdf.ln(10)

    # Order metadata
    pdf.set_font('Arial', 'B', 10)
    pdf.cell(0, 6, f"Order ID: {order_id}", 0, 1)
    pdf.cell(0, 6, f"Date: {datetime.utcnow().strftime('%Y-%m-%d')}", 0, 1)
    pdf.ln(10)

    # Customer details
    pdf.set_font('Arial', 'B', 10)
    pdf.cell(0, 6, "Deliver To:", 0, 1)
    pdf.set_font('Arial', '', 10)
    pdf.cell(0, 6, customer_data['name'], 0, 1)
    pdf.cell(0, 6, customer_data['phone'], 0, 1)
    pdf.multi_cell(0, 6, customer_data['address'])
    pdf.ln(10)

    # Items table header (no prices)
    pdf.set_font('Arial', 'B', 10)
    pdf.cell(100, 8, 'Description', 1)
    pdf.cell(30, 8, 'Quantity', 1)
    pdf.ln()

    # Items
    pdf.set_font('Arial', '', 10)
    for item in order_data.get('items', []):
        description = item['description'][:40]
        qty = item['quantity']
        pdf.cell(100, 6, description, 1)
        pdf.cell(30, 6, str(qty), 1)
        pdf.ln()

    # Save to temp file
    with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as tmp:
        pdf.output(tmp.name)
        return tmp.name

def generate_invoice_pdf(order_id, order_data, customer_data, business_data):
    pdf = FPDF()
    pdf.add_page()

    # Business details
    pdf.set_font('Arial', 'B', 12)
    pdf.cell(0, 8, business_data['name'], 0, 1)
    pdf.set_font('Arial', '', 10)
    pdf.cell(0, 6, f"Reg No: {business_data['reg_no']}", 0, 1)
    pdf.cell(0, 6, f"VAT No: {business_data['vat_id']}", 0, 1)
    pdf.cell(0, 6, business_data['address'], 0, 1)
    pdf.cell(0, 6, f"Tel: {business_data['phone']}", 0, 1)
    pdf.ln(10)

    pdf.set_font('Arial', 'B', 10)
    pdf.cell(0, 6, f"Invoice Number: INV-{order_id[-8:]}", 0, 1)
    pdf.cell(0, 6, f"Date: {datetime.utcnow().strftime('%Y-%m-%d')}", 0, 1)
    pdf.cell(0, 6, f"Order ID: {order_id}", 0, 1)
    pdf.ln(10)

    pdf.set_font('Arial', 'B', 10)
    pdf.cell(0, 6, "Bill To:", 0, 1)
    pdf.set_font('Arial', '', 10)
    pdf.cell(0, 6, customer_data['name'], 0, 1)
    pdf.cell(0, 6, customer_data['phone'], 0, 1)
    pdf.multi_cell(0, 6, customer_data['address'])
    pdf.ln(10)

    pdf.set_font('Arial', 'B', 10)
    pdf.cell(80, 8, 'Description', 1)
    pdf.cell(30, 8, 'Qty', 1)
    pdf.cell(40, 8, 'Unit Price', 1)
    pdf.cell(40, 8, 'Total', 1)
    pdf.ln()

    pdf.set_font('Arial', '', 10)
    total_ex_vat = 0
    for item in order_data.get('items', []):
        description = item['description'][:30]
        qty = item['quantity']
        unit_price = item['unit_price']
        line_total = item['line_total']
        pdf.cell(80, 6, description, 1)
        pdf.cell(30, 6, str(qty), 1)
        pdf.cell(40, 6, f"R{unit_price:.2f}", 1)
        pdf.cell(40, 6, f"R{line_total:.2f}", 1)
        pdf.ln()
        total_ex_vat += line_total

    vat_rate = 0.15
    vat_amount = total_ex_vat * vat_rate
    total_inc_vat = total_ex_vat + vat_amount

    pdf.ln(5)
    pdf.set_font('Arial', 'B', 10)
    pdf.cell(150, 8, 'Subtotal (ex VAT):', 0, 0, 'R')
    pdf.cell(40, 8, f"R{total_ex_vat:.2f}", 0, 1)
    pdf.cell(150, 8, f'VAT ({vat_rate*100}%):', 0, 0, 'R')
    pdf.cell(40, 8, f"R{vat_amount:.2f}", 0, 1)
    pdf.cell(150, 8, 'TOTAL (inc VAT):', 0, 0, 'R')
    pdf.cell(40, 8, f"R{total_inc_vat:.2f}", 0, 1)

    pdf.ln(10)
    pdf.set_font('Arial', '', 9)
    pdf.cell(0, 6, "Payment Details:", 0, 1)
    pdf.multi_cell(0, 6, business_data['bank_details'])

    with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as tmp:
        pdf.output(tmp.name)
        return tmp.name

def main():
    token = "7579196870:AAHYB3_39b50mJEC4HX2Z7K-XuzMSm5NPjs"

    app = Application.builder().token(token).build()

    loop = asyncio.get_event_loop()
    loop.run_until_complete(app.bot.set_my_commands([
        ("start", "Start the bot and show main menu"),
        ("neworder", "Place a new order"),
        ("myinvoice", "Get your most recent invoice (enter PIN)"),
        ("mydelivery", "Get a delivery note (no prices)"),
    ]))

    # Order placement conversation
    order_conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("neworder", new_order_start),
            MessageHandler(filters.Regex("^🛒 Place Order$"), new_order_start)
        ],
        states={
            SELECTING_SIZE: [CallbackQueryHandler(size_selected)],
            SELECTING_QUANTITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, quantity_entered)],
            CONFIRM_ADD_MORE: [CallbackQueryHandler(confirm_callback)],
            CHOOSE_SAVED_OR_NEW: [CallbackQueryHandler(choose_saved_or_new)],
            ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_name)],
            ASK_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_phone)],
            ASK_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_address)],
            ASK_PIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_pin)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(order_conv_handler)

    # Delivery note conversation
    delivery_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("mydelivery", mydelivery)],
        states={
            DELIVERY_WAITING_PIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_delivery_pin)]
        },
        fallbacks=[CommandHandler("cancel", cancel_delivery)],
    )
    app.add_handler(delivery_conv_handler)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("myinvoice", myinvoice))
    app.add_handler(MessageHandler(filters.Regex('^\\d{4}$'), handle_invoice_request))
    app.add_handler(MessageHandler(filters.Regex(r'^[0-9a-fA-F]{24}\s+\d{4}$'), handle_invoice_request))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.Regex("^🛒 Place Order$"), handle_invoice_request))

    print("Bot started with PDF delivery note. Press Ctrl+C to stop.")
    app.run_polling()

if __name__ == "__main__":
    main()
