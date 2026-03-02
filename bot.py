import os
import asyncio
import logging
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, ConversationHandler
import pymongo
import bcrypt
from datetime import datetime

TOKEN = os.environ.get("BOT_TOKEN")
MONGODB_URI = os.environ.get("MONGODB_URI")
PORT = int(os.environ.get("PORT", 8000))
URL = os.environ.get("RENDER_EXTERNAL_URL")

if not TOKEN or not MONGODB_URI:
    raise RuntimeError("BOT_TOKEN and MONGODB_URI must be set")

client = pymongo.MongoClient(MONGODB_URI)
db = client["paybridge"]
orders_col = db["orders"]
shops_col = db["shops"]

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Welcome to Paybridge Invoice Bot!\n\n"
        "Use /neworder to place an order."
    )

async def neworder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Starting new order...\n"
        "Select product:"
    )
    return 0

async def main():
    app = Application.builder().token(TOKEN).updater(None).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("neworder", neworder))

    webhook_url = f"{URL}/telegram"
    await app.bot.set_webhook(url=webhook_url, allowed_updates=Update.ALL_TYPES)
    logging.info(f"Webhook set to {webhook_url}")

    async def telegram(request: Request) -> Response:
        update = Update.de_json(await request.json(), app.bot)
        await app.update_queue.put(update)
        return Response()

    async def health(_: Request) -> PlainTextResponse:
        return PlainTextResponse("OK")

    starlette_app = Starlette(routes=[
        Route("/telegram", telegram, methods=["POST"]),
        Route("/healthcheck", health, methods=["GET"]),
        Route("/", health, methods=["GET"]),
    ])

    import uvicorn
    server = uvicorn.Server(
        uvicorn.Config(
            app=starlette_app,
            host="0.0.0.0",
            port=PORT,
            log_level="info"
        )
    )

    async with app:
        await app.start()
        await server.serve()
        await app.stop()

if __name__ == "__main__":
    asyncio.run(main())
