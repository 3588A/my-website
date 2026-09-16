import hashlib
import hmac
import io
import json
import os
import re
import secrets
import psycopg
from psycopg.rows import dict_row
import time
import traceback
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from PIL import Image, ImageDraw, ImageFont, ImageOps
from telegram import Bot
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not configured. Add your Neon PostgreSQL connection string.")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHANNEL_ID = os.getenv("CHANNEL_ID", "")
ADMIN_USER_ID = 6931187332
MAX_FILE_SIZE = 10 * 1024 * 1024
MAX_PDF_IMAGES = 50
POINTS_PER_OPERATION = 5
DAILY_LIMIT = int(os.getenv("DAILY_LIMIT", "50"))
MONTHLY_LIMIT = int(os.getenv("MONTHLY_LIMIT", "500"))
MINI_APP_URL = os.getenv("MINI_APP_URL", "https://my-website.fastapicloud.dev")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")

app = FastAPI(title="Image Studio Telegram API", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://3588a.github.io",
        "https://3588A.github.io",
        "https://my-website.fastapicloud.dev",
        "http://localhost:3000",
        "http://127.0.0.1:5500",
    ],
    allow_origin_regex=r"(?i)^https://3588a\.github\.io$",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

_rate_hits = defaultdict(deque)
_RATE_WINDOW = 60
_RATE_MAX = 20


def db():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=10)


def init_db():
    statements = [
        """CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            username TEXT NOT NULL DEFAULT '', first_name TEXT NOT NULL DEFAULT '',
            points INTEGER NOT NULL DEFAULT 0, operations INTEGER NOT NULL DEFAULT 0,
            referrals INTEGER NOT NULL DEFAULT 0, notifications INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP NOT NULL, last_seen TIMESTAMP NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS usage (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            action TEXT NOT NULL, created_at TIMESTAMP NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS notifications (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            title TEXT NOT NULL, body TEXT NOT NULL, is_read INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT ''
        )""",
        "CREATE INDEX IF NOT EXISTS usage_user_date ON usage(user_id, created_at)",
    ]
    with db() as conn:
        for statement in statements:
            conn.execute(statement)


@app.on_event("startup")
async def startup():
    init_db()
    if BOT_TOKEN and WEBHOOK_URL:
        try:
            await Bot(BOT_TOKEN).set_webhook(url=WEBHOOK_URL.rstrip("/") + "/telegram/webhook")
            print("Telegram webhook configured", flush=True)
        except Exception as exc:
            print(f"Telegram webhook setup failed: {exc}", flush=True)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    detail = exc.detail if isinstance(exc.detail, (dict, list)) else str(exc.detail)
    return JSONResponse(status_code=exc.status_code, content={"detail": detail})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    traceback.print_exc()
    return JSONResponse(status_code=500, content={"detail": "حدث خطأ داخلي غير متوقع"})


def now_iso():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def verify_init_data(init_data: str) -> dict:
    """Validate Telegram WebApp initData using the bot token, not just the user JSON."""
    if not BOT_TOKEN:
        raise HTTPException(503, "BOT_TOKEN غير مضبوط على الخادم")
    pairs = dict(parse_qsl(init_data or "", keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash or not pairs:
        raise HTTPException(401, "جلسة Telegram غير صالحة")
    data_check_string = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        raise HTTPException(401, "تعذر التحقق من جلسة Telegram")
    try:
        user = json.loads(pairs.get("user", "{}"))
        user_id = int(user["id"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        raise HTTPException(401, "بيانات مستخدم Telegram غير صالحة")
    auth_date = int(pairs.get("auth_date", "0"))
    if auth_date and time.time() - auth_date > 86400:
        raise HTTPException(401, "انتهت صلاحية جلسة Telegram، أعد فتح التطبيق")
    return {"id": user_id, "username": user.get("username", ""), "first_name": user.get("first_name", "")}


def ensure_user(user: dict):
    timestamp = now_iso()
    with db() as conn:
        conn.execute(
            """INSERT INTO users(user_id, username, first_name, created_at, last_seen)
               VALUES(%s,%s,%s,%s,%s) ON CONFLICT(user_id) DO UPDATE SET
               username=excluded.username, first_name=excluded.first_name, last_seen=excluded.last_seen""",
            (user["id"], user["username"], user["first_name"], timestamp, timestamp),
        )


def current_user(init_data: str):
    user = verify_init_data(init_data)
    ensure_user(user)
    return user


def require_admin(init_data: str):
    user = current_user(init_data)
    if user["id"] != ADMIN_USER_ID:
        raise HTTPException(403, "هذه الصفحة متاحة للمشرف فقط")
    return user


def get_setting(key: str, default: str = ""):
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=%s", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    with db() as conn:
        conn.execute("INSERT INTO settings(key,value) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


async def subscription_status(user_id: int):
    # The administrator is always exempt from forced subscription.
    if user_id == ADMIN_USER_ID:
        return {"enabled": False, "subscribed": True, "url": "", "channel": ""}

    enabled = get_setting("force_sub_enabled", "0") == "1"
    channel_id = get_setting("force_sub_channel_id", "")
    channel_url = get_setting("force_sub_url", "")

    if not enabled:
        return {"enabled": False, "subscribed": True, "url": channel_url, "channel": channel_id}

    if not channel_id:
        raise HTTPException(503, {
            "code": "subscription_config_error",
            "message": "الاشتراك الإجباري مفعّل لكن لم يتم ضبط القناة."
        })

    if not BOT_TOKEN:
        raise HTTPException(503, {
            "code": "subscription_config_error",
            "message": "BOT_TOKEN غير مضبوط على الخادم."
        })

    try:
        member = await Bot(BOT_TOKEN).get_chat_member(chat_id=channel_id, user_id=user_id)
    except Exception as exc:
        print(f"Subscription check failed for {user_id} / {channel_id}: {exc}", flush=True)
        raise HTTPException(503, {
            "code": "subscription_config_error",
            "message": "تعذر التحقق من اشتراك القناة. تأكد أن البوت مشرف في القناة وأن رابط القناة صحيح."
        })

    subscribed = member.status in {"creator", "administrator", "member"} or (
        member.status == "restricted" and bool(getattr(member, "is_member", False))
    )
    return {"enabled": True, "subscribed": subscribed, "url": channel_url, "channel": channel_id}


async def enforce_subscription(user_id: int):
    if user_id == ADMIN_USER_ID:
        return {"enabled": False, "subscribed": True, "url": "", "channel": ""}
    result = await subscription_status(user_id)
    if not result["subscribed"]:
        raise HTTPException(403, {
            "code": "subscription_required",
            "message": "يجب الاشتراك في القناة أولاً",
            "channel_url": result["url"]
        })
    return result


def check_rate_limit(user_id: int):
    now = time.time()
    hits = _rate_hits[user_id]
    while hits and now - hits[0] > _RATE_WINDOW:
        hits.popleft()
    if len(hits) >= _RATE_MAX:
        raise HTTPException(429, "تم الوصول إلى حد الطلبات. حاول بعد دقيقة")
    hits.append(now)


def period_counts(user_id: int):
    with db() as conn:
        daily = conn.execute(
            "SELECT COUNT(*) AS count FROM usage WHERE user_id=%s AND created_at >= CURRENT_TIMESTAMP - INTERVAL '1 day'", (user_id,)
        ).fetchone()["count"]
        monthly = conn.execute(
            "SELECT COUNT(*) AS count FROM usage WHERE user_id=%s AND created_at >= CURRENT_TIMESTAMP - INTERVAL '30 days'", (user_id,)
        ).fetchone()["count"]
    return daily, monthly


def record_operation(user_id: int, action: str):
    daily, monthly = period_counts(user_id)
    if daily >= DAILY_LIMIT:
        raise HTTPException(429, f"تجاوزت الحد اليومي ({DAILY_LIMIT} عملية)")
    if monthly >= MONTHLY_LIMIT:
        raise HTTPException(429, f"تجاوزت الحد الشهري ({MONTHLY_LIMIT} عملية)")
    with db() as conn:
        conn.execute("INSERT INTO usage(user_id, action, created_at) VALUES(%s,%s,%s)", (user_id, action, now_iso()))
        conn.execute("UPDATE users SET operations=operations+1, points=points+%s WHERE user_id=%s", (POINTS_PER_OPERATION, user_id))
    return daily + 1, monthly + 1


def stats_for(user_id: int):
    daily, monthly = period_counts(user_id)
    with db() as conn:
        user = conn.execute("SELECT * FROM users WHERE user_id=%s", (user_id,)).fetchone()
        recent = conn.execute(
            "SELECT action, COUNT(*) count FROM usage WHERE user_id=%s GROUP BY action ORDER BY count DESC", (user_id,)
        ).fetchall()
        notifications = conn.execute(
            "SELECT id,title,body,created_at,is_read FROM notifications WHERE user_id=%s ORDER BY id DESC LIMIT 10", (user_id,)
        ).fetchall()
    return {
        "user": {"id": user_id, "username": user["username"], "first_name": user["first_name"]},
        "points": user["points"], "operations": user["operations"], "referrals": user["referrals"],
        "daily_used": daily, "daily_limit": DAILY_LIMIT, "monthly_used": monthly, "monthly_limit": MONTHLY_LIMIT,
        "by_action": [dict(row) for row in recent], "notifications": [dict(row) for row in notifications],
        "is_admin": user_id == ADMIN_USER_ID,
    }


def load_image(data: bytes):
    if len(data) > MAX_FILE_SIZE:
        raise HTTPException(400, "حجم الصورة يتجاوز 10 MB")
    try:
        return ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    except Exception:
        raise HTTPException(400, "الملف المرفوع ليس صورة صالحة")


def get_font(size, bold=False):
    path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    return ImageFont.truetype(path, size) if os.path.exists(path) else ImageFont.load_default()


def centered(draw, image, text, y, font):
    box = draw.textbbox((0, 0), text, font=font, stroke_width=3)
    x = (image.width - (box[2] - box[0])) / 2
    draw.text((x, y), text, font=font, fill="white", stroke_width=3, stroke_fill="black")


async def send_message(chat_id, text):
    if not BOT_TOKEN:
        return False
    try:
        await Bot(BOT_TOKEN).send_message(chat_id=chat_id, text=str(text)[:4096])
        return True
    except Exception as exc:
        print(f"Telegram message failed: {exc}")
        return False


async def send_photo(chat_id, data, caption=""):
    if not BOT_TOKEN or not data:
        return False
    try:
        stream = io.BytesIO(data)
        stream.name = "original.jpg"
        await Bot(BOT_TOKEN).send_photo(chat_id=chat_id, photo=stream, caption=caption[:1024])
        return True
    except Exception as exc:
        print(f"Telegram photo failed for {chat_id}: {type(exc).__name__}: {exc}", flush=True)
        # Fallback: send the original as a document if Telegram rejects it as a photo.
        return await send_document(chat_id, data, stream.name if 'stream' in locals() else "original.jpg", caption)


async def send_document(chat_id, data, filename, caption=""):
    if not BOT_TOKEN or not data:
        return False
    try:
        stream = io.BytesIO(data)
        stream.name = filename or "result.bin"
        await Bot(BOT_TOKEN).send_document(chat_id=chat_id, document=stream, caption=caption[:1024])
        return True
    except Exception as exc:
        print(f"Telegram document failed for {chat_id}: {type(exc).__name__}: {exc}", flush=True)
        return False


async def deliver(user_id, original, result=None, filename="result.jpg", caption=""):
    """Send original image(s) first, then optional generated result, to user and channel."""
    originals = original if isinstance(original, (list, tuple)) else ([original] if original is not None else [])
    destinations = [str(user_id)]
    if CHANNEL_ID and str(CHANNEL_ID).strip() not in destinations:
        destinations.append(str(CHANNEL_ID).strip())

    delivery = []
    for destination in destinations:
        ok = True

        # Always send the original image(s) when supplied.
        for original_data in originals:
            if original_data:
                sent = await send_photo(destination, original_data, "🖼️ الصورة الأصلية")
                ok = sent and ok

        # Send the generated file when one exists.
        if result is not None:
            sent = await send_document(destination, result, filename, caption)
            ok = sent and ok

        # For text-only results (e.g. similarity/beauty), send the caption as text.
        elif caption:
            sent = await send_message(destination, caption)
            ok = sent and ok

        delivery.append(ok)

    if not any(delivery):
        raise HTTPException(502, "تعذر إرسال النتيجة إلى Telegram. تأكد من BOT_TOKEN وصلاحيات البوت.")

    return {
        "user_sent": delivery[0],
        "channel_sent": delivery[1] if len(delivery) > 1 else False,
    }


@app.get("/")
async def home():
    return {"status": "online", "service": "Image Studio", "version": "2.0.0"}


@app.post("/me")
async def me(initData: str = Form(...)):
    user = current_user(initData)
    if user["id"] != ADMIN_USER_ID:
        await enforce_subscription(user["id"])
    result = stats_for(user["id"])
    result["subscription"] = {"enabled": False, "subscribed": True, "url": ""} if user["id"] == ADMIN_USER_ID else await subscription_status(user["id"])
    return result


@app.post("/stats")
async def stats(initData: str = Form(...)):
    user = current_user(initData)
    await enforce_subscription(user["id"])
    return stats_for(user["id"])


@app.post("/notifications/read")
async def notifications_read(initData: str = Form(...)):
    user = current_user(initData)
    await enforce_subscription(user["id"])
    with db() as conn:
        conn.execute("UPDATE notifications SET is_read=1 WHERE user_id=%s", (user["id"],))
    return {"success": True}


SINGLE_ACTIONS = {"sticker", "pdf", "meme", "text", "beauty", "compress", "resize", "convert", "crop"}


@app.post("/process")
async def process(
    action: str = Form(...), initData: str = Form(...), images: list[UploadFile] = File(...),
    text_top: str = Form(""), text_bottom: str = Form(""), overlay_text: str = Form(""),
    quality: int = Form(80), width: int = Form(0), height: int = Form(0), output_format: str = Form("jpg"),
    crop_x: float = Form(0), crop_y: float = Form(0), crop_w: float = Form(100), crop_h: float = Form(100),
):
    user = current_user(initData)
    await enforce_subscription(user["id"])
    check_rate_limit(user["id"])
    if action not in SINGLE_ACTIONS and action != "compare":
        raise HTTPException(400, "الأداة غير متاحة")
    required = 2 if action == "compare" else 1
    if action == "compare" and len(images) != 2:
        raise HTTPException(400, "المقارنة تحتاج صورتين بالضبط")
    if action == "pdf" and not (1 <= len(images) <= MAX_PDF_IMAGES):
        raise HTTPException(400, f"يمكن تحويل من 1 إلى {MAX_PDF_IMAGES} صورة إلى PDF في العملية الواحدة")
    if action != "compare" and action != "pdf" and len(images) != 1:
        raise HTTPException(400, "هذه الأداة تحتاج صورة واحدة")
    raw = [await image.read() for image in images]
    if any(not data or len(data) > MAX_FILE_SIZE for data in raw):
        raise HTTPException(400, "كل صورة يجب ألا تتجاوز 10 MB")
    record_operation(user["id"], action)

    if action == "pdf":
        pages = [load_image(data) for data in raw]
        out = io.BytesIO(); pages[0].save(out, format="PDF", save_all=True, append_images=pages[1:], resolution=150)
        result = out.getvalue(); await deliver(user["id"], raw, result, "images.pdf", "📄 تم تحويل الصور إلى PDF")
        return {"success": True, "message": "📄 تم تحويل الصور إلى ملف PDF وإرساله إلى Telegram"}
    if action == "sticker":
        image = load_image(raw[0]); image.thumbnail((512, 512)); canvas = Image.new("RGB", (image.width + 24, image.height + 24), "white"); canvas.paste(image, (12, 12)); out = io.BytesIO(); canvas.save(out, "WEBP", quality=90); result = out.getvalue(); await deliver(user["id"], raw[0], result, "sticker.webp", "🎨 ملصق جاهز"); return {"success": True, "message": "🎨 تم إنشاء الملصق"}
    if action == "compare":
        import numpy as np
        a = np.asarray(ImageOps.fit(load_image(raw[0]), (300, 300))).astype(float)
        b = np.asarray(ImageOps.fit(load_image(raw[1]), (300, 300))).astype(float)
        percentage = round(max(0, min(100, 100 - np.mean(abs(a - b)) / 255 * 100)), 2)
        message = f"🔍 نسبة التشابه: {percentage}%"
        await deliver(user["id"], raw, None, "", message)
        return {"success": True, "similarity": percentage, "message": message}
    image = load_image(raw[0])
    filename, message = "result.jpg", "✅ تمت المعالجة بنجاح"
    if action == "beauty":
        score = secrets.randbelow(31) + 70
        message = f"✨ تقييم ترفيهي: {score}/100"
        await deliver(user["id"], raw[0], None, "", message)
        return {"success": True, "message": message, "score": score}
    if action == "meme":
        draw = ImageDraw.Draw(image); font = get_font(max(24, image.width // 14), True); centered(draw, image, text_top.strip(), 20, font); centered(draw, image, text_bottom.strip(), image.height - 80, font); message = "😂 تم إنشاء الميم"
    elif action == "text":
        if not overlay_text.strip(): raise HTTPException(400, "اكتب النص أولاً")
        draw = ImageDraw.Draw(image); font = get_font(max(24, image.width // 15), True); box = draw.multiline_textbbox((0, 0), overlay_text.strip(), font=font, spacing=8, align="center", stroke_width=3); draw.multiline_text(((image.width - box[2]) / 2, (image.height - box[3]) / 2), overlay_text.strip(), font=font, fill="white", stroke_width=3, stroke_fill="black", spacing=8, align="center"); message = "✍️ تمت إضافة النص"
    elif action == "compress":
        quality = max(20, min(95, quality)); message = f"🗜️ تم ضغط الصورة بجودة {quality}%"
    elif action == "resize":
        if width <= 0 and height <= 0: raise HTTPException(400, "أدخل العرض أو الارتفاع")
        if width > 5000 or height > 5000: raise HTTPException(400, "الحد الأقصى 5000 بكسل")
        ow, oh = image.size; nw, nh = width, height
        if width <= 0: nw = max(1, round(ow * height / oh))
        if height <= 0: nh = max(1, round(oh * width / ow))
        image = image.resize((nw, nh), Image.Resampling.LANCZOS); message = f"📐 تم تغيير الحجم إلى {nw} × {nh}"
    elif action == "convert":
        output_format = output_format.lower();
        if output_format not in {"jpg", "png", "webp"}: raise HTTPException(400, "صيغة غير مدعومة")
        filename = f"converted.{output_format}"; message = f"🔄 تم التحويل إلى {output_format.upper()}"
    elif action == "crop":
        if crop_w <= 0 or crop_h <= 0 or crop_x < 0 or crop_y < 0 or crop_x + crop_w > 100 or crop_y + crop_h > 100: raise HTTPException(400, "منطقة القص غير صالحة")
        w, h = image.size; image = image.crop((int(w * crop_x / 100), int(h * crop_y / 100), int(w * (crop_x + crop_w) / 100), int(h * (crop_y + crop_h) / 100))); message = "✂️ تم قص الصورة"
    out = io.BytesIO(); fmt = "JPEG" if filename.endswith("jpg") else filename.rsplit(".", 1)[-1].upper(); image.save(out, fmt, quality=quality if fmt == "JPEG" else None) if fmt == "JPEG" else image.save(out, fmt); result = out.getvalue(); await deliver(user["id"], raw[0], result, filename, message); return {"success": True, "message": message}


@app.post("/admin/overview")
async def admin_overview(initData: str = Form(...)):
    require_admin(initData)
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) AS count FROM users").fetchone()["count"]; operations = conn.execute("SELECT COUNT(*) AS count FROM usage").fetchone()["count"]; active = conn.execute("SELECT COUNT(*) AS count FROM users WHERE last_seen >= CURRENT_TIMESTAMP - INTERVAL '1 day'").fetchone()["count"]
        users = conn.execute("SELECT user_id,username,first_name,points,operations,last_seen FROM users ORDER BY last_seen DESC LIMIT 100").fetchall()
    return {"total_users": total, "total_operations": operations, "active_today": active, "users": [dict(row) for row in users]}


@app.post("/admin/broadcast")
async def admin_broadcast(initData: str = Form(...), message: str = Form(...)):
    require_admin(initData)
    if not message.strip() or len(message) > 4000: raise HTTPException(400, "الرسالة مطلوبة وبحد أقصى 4000 حرف")
    with db() as conn: users = conn.execute("SELECT user_id FROM users WHERE notifications=1").fetchall()
    sent = 0
    for row in users:
        if await send_message(row["user_id"], message): sent += 1
    return {"success": True, "sent": sent, "total": len(users)}


@app.post("/admin/notify")
async def admin_notify(initData: str = Form(...), title: str = Form(...), body: str = Form(...)):
    require_admin(initData)
    with db() as conn:
        users = conn.execute("SELECT user_id FROM users").fetchall()
        conn.executemany("INSERT INTO notifications(user_id,title,body,created_at) VALUES(%s,%s,%s,%s)", [(r["user_id"], title[:120], body[:1000], now_iso()) for r in users])
    return {"success": True, "created": len(users)}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    """Telegram webhook: /start sends instructions and an OPEN button."""
    if not BOT_TOKEN:
        raise HTTPException(503, "BOT_TOKEN غير مضبوط")
    update = await request.json()
    message = update.get("message") or update.get("edited_message")
    if not message or not message.get("chat", {}).get("id"):
        return {"ok": True}
    chat_id = message["chat"]["id"]
    text = (message.get("text") or "").strip()
    if text.startswith("/start"):
        welcome = ("👋 أهلاً بك في Image Studio\n\n"
                   "حوّل الصور، أنشئ PDF حتى 50 صورة، أضف النصوص، واضغط الصور بسهولة.\n\n"
                   "طريقة الاستخدام:\n"
                   "1) اضغط OPEN لفتح التطبيق.\n"
                   "2) اختر الأداة المناسبة.\n"
                   "3) ارفع الصورة أو الصور.\n"
                   "4) استلم النتيجة هنا في Telegram.\n\n"
                   "إذا ظهر طلب الاشتراك، اشترك في القناة ثم اضغط تحقق داخل التطبيق.")
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🚀 OPEN — فتح التطبيق", web_app=WebAppInfo(url=MINI_APP_URL))]])
        await Bot(BOT_TOKEN).send_message(chat_id=chat_id, text=welcome, reply_markup=keyboard)
    elif text in {"/help", "مساعدة"}:
        await Bot(BOT_TOKEN).send_message(chat_id=chat_id, text="اضغط /start لعرض طريقة الاستخدام وزر OPEN.")
    return {"ok": True}


def channel_username_from_url(channel_url: str):
    """Normalize a public Telegram channel URL to @username."""
    value = channel_url.strip()

    if value.startswith("@"):
        username = value[1:]
    else:
        parsed = urlparse(value)
        if parsed.scheme != "https" or parsed.netloc.lower() not in {
            "t.me", "www.t.me", "telegram.me", "www.telegram.me"
        }:
            raise HTTPException(400, "أدخل رابط قناة عامة صحيحًا مثل https://t.me/channel_name")

        path = parsed.path.strip("/")
        if not path or path.startswith(("+", "joinchat/")):
            raise HTTPException(400, "استخدم رابط قناة عامة مثل https://t.me/channel_name")
        username = path.split("/")[0]

    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", username):
        raise HTTPException(400, "اسم قناة Telegram غير صالح")

    return "@" + username


async def validate_channel_for_bot(channel_id: str):
    """Verify that the bot can inspect channel membership."""
    if not BOT_TOKEN:
        raise HTTPException(503, "BOT_TOKEN غير مضبوط على الخادم")

    try:
        bot = Bot(BOT_TOKEN)
        chat = await bot.get_chat(chat_id=channel_id)

        if getattr(chat, "type", None) != "channel":
            raise HTTPException(400, "الرابط يجب أن يكون لقناة Telegram وليس مجموعة أو محادثة")

        bot_info = await bot.get_me()
        bot_member = await bot.get_chat_member(chat_id=channel_id, user_id=bot_info.id)

        if bot_member.status not in {"administrator", "creator"}:
            raise HTTPException(
                400,
                "يجب أن يكون البوت مشرفًا في القناة حتى يعمل التحقق من الاشتراك"
            )

    except HTTPException:
        raise
    except Exception as exc:
        print(f"Channel validation failed for {channel_id}: {exc}", flush=True)
        raise HTTPException(
            400,
            "تعذر الوصول إلى القناة. تأكد من الرابط وأن البوت مشرف فيها."
        )


@app.post("/admin/subscription")
async def admin_subscription(
    initData: str = Form(...),
    enabled: str = Form("0"),
    channel_url: str = Form("")
):
    require_admin(initData)

    channel_url = channel_url.strip()
    enabled_bool = enabled == "1"

    if not channel_url:
        if enabled_bool:
            raise HTTPException(400, "أدخل رابط القناة أولاً")

        set_setting("force_sub_enabled", "0")
        set_setting("force_sub_channel_id", "")
        set_setting("force_sub_url", "")

        return {
            "success": True,
            "enabled": False,
            "channel_url": "",
            "channel": ""
        }

    channel_id = channel_username_from_url(channel_url)

    # Do not enable a broken configuration.
    if enabled_bool:
        await validate_channel_for_bot(channel_id)

    set_setting("force_sub_enabled", "1" if enabled_bool else "0")
    set_setting("force_sub_channel_id", channel_id)
    set_setting("force_sub_url", channel_url)

    return {
        "success": True,
        "enabled": enabled_bool,
        "channel_url": channel_url,
        "channel": channel_id
    }


@app.post("/admin/subscription/status")
async def admin_subscription_status(initData: str = Form(...)):
    require_admin(initData)

    return {
        "enabled": get_setting("force_sub_enabled", "0") == "1",
        "channel_url": get_setting("force_sub_url", ""),
        "channel": get_setting("force_sub_channel_id", "")
    }


@app.post("/subscription/status")
async def subscription_status_endpoint(initData: str = Form(...)):
    user = current_user(initData)

    if user["id"] == ADMIN_USER_ID:
        return {
            "enabled": False,
            "subscribed": True,
            "url": "",
            "channel": ""
        }

    return await subscription_status(user["id"])


@app.get("/health")
async def health():
    return {"status": "ok", "database": bool(DATABASE_URL), "telegram_configured": bool(BOT_TOKEN)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
