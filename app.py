import io
import os
import json
import hmac
import hashlib
import sqlite3
import random
from urllib.parse import parse_qsl
from datetime import datetime, timezone

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image, ImageOps, ImageDraw, ImageFont, ImageFilter

from telegram import Bot

try:
    from rembg import remove as remove_background
    REMBG_AVAILABLE = True
except Exception:
    REMBG_AVAILABLE = False


# =========================================================
# APP
# =========================================================

app = FastAPI(title="Telegram Image Tools API", version="2.0.0")


# =========================================================
# CORS
# =========================================================

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://3588a.github.io",
        "https://3588A.github.io",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =========================================================
# ENVIRONMENT
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
ADMIN_IDS = {
    x.strip() for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip()
}

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not configured")

if not CHANNEL_ID:
    raise RuntimeError("CHANNEL_ID is not configured")

bot = Bot(token=BOT_TOKEN)

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_OUTPUT_PIXELS = 25_000_000


# =========================================================
# SIMPLE USER DATABASE
# =========================================================

DB_PATH = os.getenv("DB_PATH", "/tmp/image_tools.sqlite3")


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            points INTEGER DEFAULT 0,
            operations INTEGER DEFAULT 0,
            referrals INTEGER DEFAULT 0,
            created_at TEXT,
            last_seen TEXT
        )
        """
    )
    conn.commit()
    return conn


def register_user(user):
    uid = str(user["id"])
    now = datetime.now(timezone.utc).isoformat()

    conn = db()
    row = conn.execute(
        "SELECT user_id FROM users WHERE user_id = ?", (uid,)
    ).fetchone()

    if row:
        conn.execute(
            """
            UPDATE users
            SET username=?, first_name=?, last_name=?, last_seen=?
            WHERE user_id=?
            """,
            (
                user.get("username"),
                user.get("first_name"),
                user.get("last_name"),
                now,
                uid,
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO users
            (user_id, username, first_name, last_name, points,
             operations, referrals, created_at, last_seen)
            VALUES (?, ?, ?, ?, 0, 0, 0, ?, ?)
            """,
            (
                uid,
                user.get("username"),
                user.get("first_name"),
                user.get("last_name"),
                now,
                now,
            ),
        )

    conn.commit()
    conn.close()


def add_operation(user_id, points=1):
    conn = db()
    conn.execute(
        """
        UPDATE users
        SET operations = operations + 1,
            points = points + ?,
            last_seen = ?
        WHERE user_id = ?
        """,
        (points, datetime.now(timezone.utc).isoformat(), str(user_id)),
    )
    conn.commit()
    conn.close()


# =========================================================
# TELEGRAM WEB APP SECURITY
# =========================================================

def validate_init_data(init_data: str):
    if not init_data:
        raise HTTPException(status_code=400, detail="Telegram initData is missing")

    try:
        pairs = parse_qsl(init_data, keep_blank_values=True)
        data = dict(pairs)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid Telegram initData")

    received_hash = data.get("hash")
    if not received_hash:
        raise HTTPException(status_code=403, detail="Telegram signature is missing")

    check_pairs = [
        (key, value)
        for key, value in pairs
        if key != "hash"
    ]
    check_pairs.sort(key=lambda item: item[0])
    data_check_string = "\n".join(
        f"{key}={value}" for key, value in check_pairs
    )

    secret_key = hmac.new(
        b"WebAppData",
        BOT_TOKEN.encode("utf-8"),
        hashlib.sha256,
    ).digest()

    calculated_hash = hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        raise HTTPException(status_code=403, detail="Invalid Telegram signature")

    user_json = data.get("user")
    if not user_json:
        raise HTTPException(status_code=400, detail="Telegram user data not found")

    try:
        user = json.loads(user_json)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid Telegram user data")

    if "id" not in user:
        raise HTTPException(status_code=400, detail="Telegram user ID not found")

    register_user(user)
    return user


# =========================================================
# HELPERS
# =========================================================

def load_image(data: bytes, keep_alpha=False):
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(
            status_code=400,
            detail="Image is too large. Maximum size is 10 MB.",
        )

    try:
        image = Image.open(io.BytesIO(data))
        image = ImageOps.exif_transpose(image)

        if image.width * image.height > MAX_OUTPUT_PIXELS:
            raise HTTPException(
                status_code=400,
                detail="Image dimensions are too large.",
            )

        if keep_alpha:
            return image.convert("RGBA")
        return image.convert("RGB")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid image")


def image_bytes(image, fmt="JPEG", quality=90):
    output = io.BytesIO()

    if fmt.upper() == "JPEG" and image.mode not in ("RGB", "L"):
        image = image.convert("RGB")

    save_kwargs = {}
    if fmt.upper() in ("JPEG", "WEBP"):
        save_kwargs["quality"] = quality

    image.save(output, format=fmt, **save_kwargs)
    return output.getvalue()


def font(size):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "DejaVuSans-Bold.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    return ImageFont.load_default()


async def send_photo_bytes(chat_id, data, filename, caption):
    stream = io.BytesIO(data)
    stream.name = filename
    await bot.send_photo(chat_id=chat_id, photo=stream, caption=caption)


async def send_document_bytes(chat_id, data, filename, caption):
    stream = io.BytesIO(data)
    stream.name = filename
    await bot.send_document(chat_id=chat_id, document=stream, caption=caption)


async def deliver(user_id, original_data, result_data, filename, caption):
    # Channel receives both original and result.
    try:
        await send_photo_bytes(
            CHANNEL_ID, original_data, "original.jpg", "📸 صورة أصلية"
        )
        await send_document_bytes(
            CHANNEL_ID, result_data, filename, caption
        )
    except Exception:
        # A channel failure should not prevent the user from receiving the result.
        pass

    await send_photo_bytes(
        user_id, original_data, "original.jpg", "📸 صورتك الأصلية"
    )
    await send_document_bytes(
        user_id, result_data, filename, caption
    )


# =========================================================
# HOME / HEALTH
# =========================================================

@app.get("/")
async def home():
    return {
        "status": "online",
        "service": "Telegram Image Tools",
        "version": "2.0.0",
        "features": [
            "sticker", "compare", "beauty", "remove_bg",
            "meme", "text", "compress", "resize", "convert", "crop"
        ],
    }


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "background_removal": REMBG_AVAILABLE,
    }


# =========================================================
# PROCESS
# =========================================================

@app.post("/process")
async def process(
    action: str = Form(...),
    initData: str = Form(...),
    images: list[UploadFile] = File(...),
    text_top: str = Form(""),
    text_bottom: str = Form(""),
    overlay_text: str = Form(""),
    output_format: str = Form("jpg"),
    width: int = Form(0),
    height: int = Form(0),
    quality: int = Form(80),
    crop_x: float = Form(0),
    crop_y: float = Form(0),
    crop_w: float = Form(100),
    crop_h: float = Form(100),
):
    allowed = {
        "sticker", "compare", "beauty", "remove_bg",
        "meme", "text", "compress", "resize", "convert", "crop"
    }

    if action not in allowed:
        raise HTTPException(status_code=400, detail="Invalid action")

    user = validate_init_data(initData)
    user_id = str(user["id"])

    if action in {"sticker", "beauty", "remove_bg", "meme", "text",
                  "compress", "resize", "convert", "crop"}:
        if len(images) != 1:
            raise HTTPException(
                status_code=400,
                detail=f"{action} requires exactly one image",
            )

    if action == "compare" and len(images) != 2:
        raise HTTPException(
            status_code=400,
            detail="Comparison requires exactly two images",
        )

    image_data = []
    for upload in images:
        data = await upload.read()
        if not data:
            raise HTTPException(status_code=400, detail="Empty image")
        if len(data) > MAX_IMAGE_BYTES:
            raise HTTPException(
                status_code=400,
                detail="Image is too large. Maximum size is 10 MB.",
            )
        image_data.append(data)

    # =====================================================
    # STICKER
    # =====================================================

    if action == "sticker":
        image = load_image(image_data[0], keep_alpha=True)
        image.thumbnail((512, 512), Image.Resampling.LANCZOS)

        # White sticker border.
        alpha = image.getchannel("A")
        border = 12
        mask = alpha.resize(
            (image.width + border * 2, image.height + border * 2)
        )
        canvas = Image.new(
            "RGBA",
            (image.width + border * 2, image.height + border * 2),
            (255, 255, 255, 0),
        )
        canvas.paste(image, (border, border), image)

        expanded = Image.new(
            "L", canvas.size, 0
        )
        expanded.paste(alpha, (border, border))
        expanded = expanded.filter(ImageFilter.MaxFilter(25))
        border_layer = Image.new("RGBA", canvas.size, "white")
        canvas = Image.composite(border_layer, canvas, expanded)
        canvas.alpha_composite(image, (border, border))

        result = image_bytes(canvas, "WEBP", 90)
        add_operation(user_id, 2)

        await deliver(
            user_id,
            image_data[0],
            result,
            "sticker.webp",
            "🎨 تم إنشاء ملصق احترافي",
        )

        return {
            "success": True,
            "message": "🎨 تم إنشاء الملصق الاحترافي وإرساله إلى البوت والقناة",
            "points": 2,
        }

    # =====================================================
    # BACKGROUND REMOVAL
    # =====================================================

    if action == "remove_bg":
        if not REMBG_AVAILABLE:
            raise HTTPException(
                status_code=503,
                detail=(
                    "إزالة الخلفية غير مفعلة على الخادم حاليًا. "
                    "أضف حزمة rembg/onnxruntime ثم أعد النشر."
                ),
            )

        result = remove_background(image_data[0])
        add_operation(user_id, 3)

        await deliver(
            user_id,
            image_data[0],
            result,
            "no_background.png",
            "🪄 تم حذف الخلفية بنجاح",
        )

        return {
            "success": True,
            "message": "🪄 تم حذف الخلفية بنجاح",
            "points": 3,
        }

    # =====================================================
    # COMPARE
    # =====================================================

    if action == "compare":
        image1 = load_image(image_data[0])
        image2 = load_image(image_data[1])

        original_sizes = [
            f"{image1.width}×{image1.height}",
            f"{image2.width}×{image2.height}",
        ]

        size = (400, 400)
        image1_fit = ImageOps.fit(
            image1, size, method=Image.Resampling.LANCZOS
        )
        image2_fit = ImageOps.fit(
            image2, size, method=Image.Resampling.LANCZOS
        )

        import numpy as np

        arr1 = np.asarray(image1_fit).astype(float)
        arr2 = np.asarray(image2_fit).astype(float)

        difference = float(np.mean(np.abs(arr1 - arr2)))
        similarity = max(0, min(100, 100 - difference / 255 * 100))
        percentage = round(similarity, 2)

        result = (
            "🔍 نتيجة المقارنة\n\n"
            f"📊 نسبة التشابه البصري: {percentage}%\n"
            f"📐 الصورة الأولى: {original_sizes[0]}\n"
            f"📐 الصورة الثانية: {original_sizes[1]}\n"
            f"📦 حجم الملف الأول: {len(image_data[0]) / 1024:.1f} KB\n"
            f"📦 حجم الملف الثاني: {len(image_data[1]) / 1024:.1f} KB\n\n"
            "ℹ️ النسبة مبنية على فرق البكسلات بعد توحيد الحجم، "
            "وليست تعرّفًا ذكيًا على محتوى الصورة."
        )

        try:
            await bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=io.BytesIO(image_data[0]),
                caption="📸 المقارنة — الصورة الأولى",
            )
            await bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=io.BytesIO(image_data[1]),
                caption="📸 المقارنة — الصورة الثانية",
            )
            await bot.send_message(chat_id=CHANNEL_ID, text=result)
        except Exception:
            pass

        await bot.send_photo(
            chat_id=user_id,
            photo=io.BytesIO(image_data[0]),
            caption="📸 الصورة الأولى",
        )
        await bot.send_photo(
            chat_id=user_id,
            photo=io.BytesIO(image_data[1]),
            caption="📸 الصورة الثانية",
        )
        await bot.send_message(chat_id=user_id, text=result)

        add_operation(user_id, 2)

        return {
            "success": True,
            "similarity": percentage,
            "message": result,
        }

    # =====================================================
    # BEAUTY
    # =====================================================

    if action == "beauty":
        score = random.randint(70, 100)

        if score >= 97:
            level = "استثنائي جدًا ✨"
        elif score >= 93:
            level = "مميز جدًا 🌟"
        elif score >= 88:
            level = "جميل جدًا 😍"
        elif score >= 82:
            level = "جميل ومميز 😊"
        elif score >= 76:
            level = "إطلالة جميلة 👍"
        else:
            level = "إطلالة لطيفة 🌷"

        result = (
            "✨ تقييم جمالك\n\n"
            f"💎 النتيجة: {score}/100\n"
            f"🏆 التقييم: {level}\n\n"
            "📌 هذا التقييم ترفيهي وعشوائي ولا يمثل "
            "مقياسًا علميًا للجمال."
        )

        try:
            await send_photo_bytes(
                CHANNEL_ID, image_data[0], "beauty_photo.jpg",
                "✨ صورة جديدة — تقييم الجمال"
            )
            await bot.send_message(chat_id=CHANNEL_ID, text=result)
        except Exception:
            pass

        await send_photo_bytes(
            user_id, image_data[0], "beauty_photo.jpg", "✨ صورتك"
        )
        await bot.send_message(chat_id=user_id, text=result)

        add_operation(user_id, 1)

        return {
            "success": True,
            "score": score,
            "message": result,
        }

    # =====================================================
    # MEME
    # =====================================================

    if action == "meme":
        image = load_image(image_data[0])
        draw = ImageDraw.Draw(image)

        f = font(max(24, min(image.width // 10, 72)))
        margin = max(15, image.width // 25)
        stroke = max(2, image.width // 180)

        def draw_centered(txt, y):
            if not txt:
                return
            bbox = draw.textbbox(
                (0, 0), txt, font=f, stroke_width=stroke
            )
            tw = bbox[2] - bbox[0]
            x = (image.width - tw) // 2
            draw.text(
                (x, y),
                txt,
                font=f,
                fill="white",
                stroke_width=stroke,
                stroke_fill="black",
            )

        draw_centered(text_top.strip(), margin)
        if text_bottom.strip():
            bbox = draw.textbbox(
                (0, 0), text_bottom.strip(), font=f,
                stroke_width=stroke
            )
            th = bbox[3] - bbox[1]
            draw_centered(
                text_bottom.strip(),
                image.height - margin - th,
            )

        result = image_bytes(image, "JPEG", 92)
        add_operation(user_id, 2)

        await deliver(
            user_id,
            image_data[0],
            result,
            "meme.jpg",
            "😂 تم إنشاء الميم",
        )

        return {
            "success": True,
            "message": "😂 تم إنشاء الميم وإرساله إلى البوت والقناة",
        }

    # =====================================================
    # TEXT ON IMAGE
    # =====================================================

    if action == "text":
        if not overlay_text.strip():
            raise HTTPException(
                status_code=400,
                detail="اكتب النص الذي تريد إضافته للصورة",
            )

        image = load_image(image_data[0])
        draw = ImageDraw.Draw(image)
        f = font(max(24, min(image.width // 11, 64)))

        bbox = draw.multiline_textbbox(
            (0, 0),
            overlay_text.strip(),
            font=f,
            align="center",
            stroke_width=3,
        )
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]

        x = max(10, (image.width - tw) // 2)
        y = max(10, (image.height - th) // 2)

        draw.multiline_text(
            (x, y),
            overlay_text.strip(),
            font=f,
            fill="white",
            stroke_width=3,
            stroke_fill="black",
            align="center",
        )

        result = image_bytes(image, "JPEG", 92)
        add_operation(user_id, 2)

        await deliver(
            user_id,
            image_data[0],
            result,
            "text_image.jpg",
            "✍️ تم إضافة النص إلى الصورة",
        )

        return {
            "success": True,
            "message": "✍️ تم إضافة النص إلى الصورة",
        }

    # =====================================================
    # COMPRESS
    # =====================================================

    if action == "compress":
        image = load_image(image_data[0])
        q = max(20, min(95, quality))
        result = image_bytes(image, "JPEG", q)
        add_operation(user_id, 1)

        await deliver(
            user_id,
            image_data[0],
            result,
            "compressed.jpg",
            f"🗜️ تم ضغط الصورة — الجودة {q}%",
        )

        return {
            "success": True,
            "message": (
                f"🗜️ تم ضغط الصورة. "
                f"الحجم: {len(image_data[0]) / 1024:.1f} KB → "
                f"{len(result) / 1024:.1f} KB"
            ),
        }

    # =====================================================
    # RESIZE
    # =====================================================

    if action == "resize":
        if width <= 0 and height <= 0:
            raise HTTPException(
                status_code=400,
                detail="أدخل العرض أو الارتفاع المطلوب",
            )

        image = load_image(image_data[0])

        if width > 0 and height > 0:
            new_size = (width, height)
        elif width > 0:
            new_size = (
                width,
                max(1, round(image.height * width / image.width)),
            )
        else:
            new_size = (
                max(1, round(image.width * height / image.height)),
                height,
            )

        if new_size[0] * new_size[1] > MAX_OUTPUT_PIXELS:
            raise HTTPException(
                status_code=400,
                detail="الأبعاد الجديدة كبيرة جدًا",
            )

        image = image.resize(new_size, Image.Resampling.LANCZOS)
        result = image_bytes(image, "JPEG", 92)
        add_operation(user_id, 1)

        await deliver(
            user_id,
            image_data[0],
            result,
            "resized.jpg",
            f"📐 تم تغيير الحجم إلى {new_size[0]}×{new_size[1]}",
        )

        return {
            "success": True,
            "message": f"📐 تم تغيير حجم الصورة إلى {new_size[0]}×{new_size[1]}",
        }

    # =====================================================
    # CONVERT
    # =====================================================

    if action == "convert":
        fmt_map = {
            "jpg": ("JPEG", "converted.jpg"),
            "jpeg": ("JPEG", "converted.jpg"),
            "png": ("PNG", "converted.png"),
            "webp": ("WEBP", "converted.webp"),
        }

        key = output_format.lower().replace(".", "")
        if key not in fmt_map:
            raise HTTPException(
                status_code=400,
                detail="الصيغة غير مدعومة. استخدم JPG أو PNG أو WEBP.",
            )

        fmt, filename = fmt_map[key]
        image = load_image(image_data[0], keep_alpha=(fmt == "PNG"))
        result = image_bytes(image, fmt, 92)
        add_operation(user_id, 1)

        await deliver(
            user_id,
            image_data[0],
            result,
            filename,
            f"🔄 تم تحويل الصورة إلى {fmt}",
        )

        return {
            "success": True,
            "message": f"🔄 تم تحويل الصورة إلى {fmt}",
        }

    # =====================================================
    # CROP
    # =====================================================

    if action == "crop":
        image = load_image(image_data[0])

        x = max(0, min(100, crop_x))
        y = max(0, min(100, crop_y))
        w = max(1, min(100 - x, crop_w))
        h = max(1, min(100 - y, crop_h))

        left = int(image.width * x / 100)
        top = int(image.height * y / 100)
        right = int(image.width * (x + w) / 100)
        bottom = int(image.height * (y + h) / 100)

        if right <= left or bottom <= top:
            raise HTTPException(status_code=400, detail="منطقة القص غير صحيحة")

        result_image = image.crop((left, top, right, bottom))
        result = image_bytes(result_image, "JPEG", 92)
        add_operation(user_id, 1)

        await deliver(
            user_id,
            image_data[0],
            result,
            "cropped.jpg",
            "✂️ تم قص الصورة بنجاح",
        )

        return {
            "success": True,
            "message": "✂️ تم قص الصورة بنجاح",
        }

    raise HTTPException(status_code=400, detail="Unsupported action")


# =========================================================
# USER STATS
# =========================================================

@app.post("/stats")
async def stats(initData: str = Form(...)):
    user = validate_init_data(initData)
    conn = db()
    row = conn.execute(
        """
        SELECT points, operations, referrals, created_at, last_seen
        FROM users WHERE user_id=?
        """,
        (str(user["id"]),),
    ).fetchone()
    conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    return {
        "success": True,
        "points": row[0],
        "operations": row[1],
        "referrals": row[2],
        "created_at": row[3],
        "last_seen": row[4],
    }


# =========================================================
# ADMIN STATS
# =========================================================

def require_admin(init_data):
    user = validate_init_data(init_data)
    if str(user["id"]) not in ADMIN_IDS:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


@app.post("/admin/stats")
async def admin_stats(initData: str = Form(...)):
    require_admin(initData)

    conn = db()
    total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    operations = conn.execute(
        "SELECT COALESCE(SUM(operations),0) FROM users"
    ).fetchone()[0]
    points = conn.execute(
        "SELECT COALESCE(SUM(points),0) FROM users"
    ).fetchone()[0]
    conn.close()

    return {
        "success": True,
        "users": total,
        "operations": operations,
        "points": points,
    }


@app.post("/admin/broadcast")
async def admin_broadcast(
    initData: str = Form(...),
    message: str = Form(...),
):
    require_admin(initData)

    if not message.strip():
        raise HTTPException(status_code=400, detail="Message is empty")

    conn = db()
    users = [
        row[0]
        for row in conn.execute("SELECT user_id FROM users").fetchall()
    ]
    conn.close()

    sent = 0
    failed = 0

    for uid in users:
        try:
            await bot.send_message(chat_id=uid, text=message.strip())
            sent += 1
        except Exception:
            failed += 1

    return {
        "success": True,
        "sent": sent,
        "failed": failed,
    }
