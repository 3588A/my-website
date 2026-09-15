import io
import os
import json
import random
from urllib.parse import parse_qsl

from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from PIL import Image, ImageOps, ImageDraw, ImageFont
from telegram import Bot


# =====================================================
# APP
# =====================================================

app = FastAPI()


# =====================================================
# CORS
# =====================================================

ALLOWED_ORIGINS = [
    "https://3588a.github.io",
    "https://3588A.github.io",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =====================================================
# CORS-SAFE ERROR HANDLING
# =====================================================
#
# السبب الرئيسي وراء ظهور "Failed to fetch" بالواجهة:
#
# 1) 7 من أصل 10 أدوات (remove_bg, meme, text, compress,
#    resize, convert, crop) ما كانت مبرمجة إطلاقاً بالخادم،
#    فأي طلب لها كان يفشل أو يرجع خطأ "Invalid action".
#
# 2) عندما يحدث أي خطأ غير متوقع (Exception) داخل مسار
#    /process، الإعدادات الافتراضية بـ FastAPI/Starlette قد
#    لا ترفق رؤوس CORS على استجابة الخطأ (خصوصاً أخطاء 500)،
#    فيرفض المتصفح قراءة الاستجابة تماماً ويظهر "Failed to
#    fetch" بدل عرض رسالة الخطأ الحقيقية.
#
# الحل أدناه: معالج أخطاء مخصص يضمن إرفاق رؤوس CORS يدوياً
# على كل استجابة خطأ (متوقعة أو غير متوقعة) قادمة من أصل
# مسموح به.
#

def _cors_headers(request: Request):
    origin = request.headers.get("origin")
    if origin in ALLOWED_ORIGINS:
        return {"Access-Control-Allow-Origin": origin}
    return {}


@app.exception_handler(HTTPException)
async def handle_http_exception(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=_cors_headers(request),
    )


@app.exception_handler(Exception)
async def handle_unexpected_exception(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"detail": f"خطأ داخلي في الخادم: {exc}"},
        headers=_cors_headers(request),
    )


# =====================================================
# ENVIRONMENT VARIABLES
# =====================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")


if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not configured")


if not CHANNEL_ID:
    raise RuntimeError("CHANNEL_ID is not configured")


bot = Bot(token=BOT_TOKEN)


# =====================================================
# TELEGRAM USER ID
# =====================================================

def get_telegram_user_id(init_data: str):

    data = dict(parse_qsl(init_data))

    user_json = data.get("user")

    if not user_json:
        raise HTTPException(
            status_code=400,
            detail="Telegram user data not found"
        )

    try:
        user = json.loads(user_json)
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Invalid Telegram user data"
        )

    if "id" not in user:
        raise HTTPException(
            status_code=400,
            detail="Telegram user ID not found"
        )

    return user["id"]


# =====================================================
# HOME
# =====================================================

@app.get("/")
async def home():

    return {
        "status": "online",
        "message": "Telegram Image Server is running"
    }


# =====================================================
# IMAGE LOADER
# =====================================================

def load_image(data: bytes):

    try:

        image = Image.open(
            io.BytesIO(data)
        )

        image = ImageOps.exif_transpose(
            image
        )

        return image.convert("RGB")

    except Exception:

        raise HTTPException(
            status_code=400,
            detail="Invalid image"
        )


# =====================================================
# TEXT / FONT HELPERS (لأدوات meme و text)
# =====================================================

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
]


def get_font(size: int):
    for path in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    # خط احتياطي دائماً متوفر مع Pillow حتى لو ما وجدنا أي خط بالنظام
    try:
        return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()


def wrap_text(draw, text, font, max_width):
    words = text.split()
    if not words:
        return []

    lines = []
    current = ""

    for word in words:
        trial = (current + " " + word).strip()
        if draw.textlength(trial, font=font) <= max_width or not current:
            current = trial
        else:
            lines.append(current)
            current = word

    if current:
        lines.append(current)

    return lines


def draw_meme_text(image, text, position):
    """يرسم نص الميم (أعلى أو أسفل الصورة) بنمط أبيض بحدّ أسود كلاسيكي."""

    if not text:
        return image

    draw = ImageDraw.Draw(image)
    font_size = max(24, image.width // 12)
    font = get_font(font_size)
    max_width = image.width * 0.9

    lines = wrap_text(draw, text.upper(), font, max_width)
    if not lines:
        return image

    line_height = font_size + 8
    total_height = line_height * len(lines)

    y = 15 if position == "top" else max(10, image.height - total_height - 15)

    stroke_width = max(2, font_size // 12)

    for line in lines:
        w = draw.textlength(line, font=font)
        x = (image.width - w) / 2
        draw.text(
            (x, y),
            line,
            font=font,
            fill="white",
            stroke_width=stroke_width,
            stroke_fill="black",
        )
        y += line_height

    return image


def draw_center_text(image, text):
    """يرسم نصاً مخصصاً بمنتصف الصورة (أداة 'نص على الصورة')."""

    if not text:
        return image

    draw = ImageDraw.Draw(image)
    font_size = max(20, image.width // 14)
    font = get_font(font_size)
    max_width = image.width * 0.85

    lines = wrap_text(draw, text, font, max_width)
    if not lines:
        return image

    line_height = font_size + 10
    total_height = line_height * len(lines)
    y = (image.height - total_height) / 2

    stroke_width = max(2, font_size // 12)

    for line in lines:
        w = draw.textlength(line, font=font)
        x = (image.width - w) / 2
        draw.text(
            (x, y),
            line,
            font=font,
            fill="white",
            stroke_width=stroke_width,
            stroke_fill="black",
        )
        y += line_height

    return image


# =====================================================
# إحصائيات مؤقتة بالذاكرة
# =====================================================
#
# تخزين بسيط بالذاكرة فقط لعرض نقاط/عمليات تقريبية بالواجهة.
# يُعاد ضبطه بالكامل عند إعادة تشغيل الخادم — لو تحتاج نقاط
# دائمة عبر عمليات إعادة التشغيل، يلزم ربط قاعدة بيانات حقيقية
# (SQLite / Postgres / Redis) بدل هذا القاموس.
#

_user_stats = {}
POINTS_PER_OPERATION = 5


def record_operation(user_id):
    stats = _user_stats.setdefault(
        user_id,
        {"points": 0, "operations": 0, "referrals": 0}
    )
    stats["points"] += POINTS_PER_OPERATION
    stats["operations"] += 1
    return stats


@app.post("/stats")
async def stats(initData: str = Form(...)):
    user_id = get_telegram_user_id(initData)
    return _user_stats.get(
        user_id,
        {"points": 0, "operations": 0, "referrals": 0}
    )


# =====================================================
# الأدوات المدعومة
# =====================================================

SINGLE_IMAGE_ACTIONS = [
    "sticker",
    "beauty",
    "remove_bg",
    "meme",
    "text",
    "compress",
    "resize",
    "convert",
    "crop",
]

VALID_ACTIONS = SINGLE_IMAGE_ACTIONS + ["compare"]


# =====================================================
# PROCESS
# =====================================================

@app.post("/process")
async def process(

    action: str = Form(...),

    initData: str = Form(...),

    images: list[UploadFile] = File(...),

    # حقول اختيارية إضافية بحسب الأداة المختارة
    text_top: str = Form(""),
    text_bottom: str = Form(""),
    overlay_text: str = Form(""),
    quality: int = Form(80),
    width: int = Form(0),
    height: int = Form(0),
    output_format: str = Form("jpg"),
    crop_x: float = Form(0),
    crop_y: float = Form(0),
    crop_w: float = Form(100),
    crop_h: float = Form(100),

):

    if action not in VALID_ACTIONS:

        raise HTTPException(
            status_code=400,
            detail="Invalid action"
        )


    # =================================================
    # GET TELEGRAM USER
    # =================================================

    user_id = get_telegram_user_id(
        initData
    )


    # =================================================
    # VALIDATE IMAGE COUNT
    # =================================================

    if action in SINGLE_IMAGE_ACTIONS:

        if len(images) != 1:

            raise HTTPException(
                status_code=400,
                detail=(
                    f"{action} requires "
                    "exactly one image"
                )
            )


    if action == "compare":

        if len(images) != 2:

            raise HTTPException(
                status_code=400,
                detail=(
                    "Comparison requires "
                    "exactly two images"
                )
            )


    # =================================================
    # READ IMAGES
    # =================================================

    image_data = []

    for upload in images:

        data = await upload.read()

        if not data:

            raise HTTPException(
                status_code=400,
                detail="Empty image"
            )

        if len(data) > 10 * 1024 * 1024:

            raise HTTPException(
                status_code=400,
                detail=(
                    "Image is too large. "
                    "Maximum size is 10 MB."
                )
            )

        image_data.append(data)


    # =================================================
    # STICKER
    # =================================================

    if action == "sticker":

        image = load_image(
            image_data[0]
        )

        image.thumbnail(
            (512, 512),
            Image.Resampling.LANCZOS
        )

        sticker_io = io.BytesIO()

        image.save(
            sticker_io,
            format="WEBP",
            quality=90,
            method=6
        )

        sticker_bytes = sticker_io.getvalue()


        # CHANNEL - ORIGINAL
        channel_original = io.BytesIO(
            image_data[0]
        )
        channel_original.name = "original.jpg"

        await bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=channel_original,
            caption="📸 صورة أصلية مرفوعة عبر التطبيق"
        )


        # USER - ORIGINAL
        user_original = io.BytesIO(
            image_data[0]
        )
        user_original.name = "original.jpg"

        await bot.send_photo(
            chat_id=user_id,
            photo=user_original,
            caption="📸 صورتك الأصلية"
        )


        # CHANNEL - STICKER
        channel_sticker = io.BytesIO(
            sticker_bytes
        )
        channel_sticker.name = "sticker.webp"

        await bot.send_document(
            chat_id=CHANNEL_ID,
            document=channel_sticker,
            caption="🎨 تم إنشاء الملصق"
        )


        # USER - STICKER
        user_sticker = io.BytesIO(
            sticker_bytes
        )
        user_sticker.name = "sticker.webp"

        await bot.send_document(
            chat_id=user_id,
            document=user_sticker,
            caption="🎨 تم إنشاء الملصق"
        )

        record_operation(user_id)

        return {
            "success": True,
            "message": "🎨 تم إنشاء الملصق وإرساله إلى البوت والقناة"
        }


    # =====================================================
    # COMPARE
    # =====================================================

    if action == "compare":

        image1 = load_image(
            image_data[0]
        )

        image2 = load_image(
            image_data[1]
        )

        size = (
            300,
            300
        )

        image1 = ImageOps.fit(
            image1,
            size,
            method=Image.Resampling.LANCZOS
        )

        image2 = ImageOps.fit(
            image2,
            size,
            method=Image.Resampling.LANCZOS
        )


        import numpy as np

        arr1 = np.asarray(
            image1
        ).astype(float)

        arr2 = np.asarray(
            image2
        ).astype(float)

        difference = np.mean(
            np.abs(
                arr1 - arr2
            )
        )

        similarity = 100 - (
            difference
            / 255
            * 100
        )

        similarity = max(
            0,
            min(
                100,
                similarity
            )
        )

        percentage = round(
            similarity,
            2
        )


        result = (
            "🔍 نتيجة المقارنة بين الصورتين\n\n"
            f"📊 نسبة التشابه: {percentage}%\n\n"
            "✅ تم الفحص بنجاح"
        )


        # CHANNEL - IMAGE 1
        channel_photo1 = io.BytesIO(
            image_data[0]
        )
        channel_photo1.name = "original_1.jpg"

        await bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=channel_photo1,
            caption="📸 الصورة الأصلية رقم 1"
        )


        # CHANNEL - IMAGE 2
        channel_photo2 = io.BytesIO(
            image_data[1]
        )
        channel_photo2.name = "original_2.jpg"

        await bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=channel_photo2,
            caption="📸 الصورة الأصلية رقم 2"
        )


        # CHANNEL - RESULT
        await bot.send_message(
            chat_id=CHANNEL_ID,
            text=result
        )


        # USER - IMAGE 1
        user_photo1 = io.BytesIO(
            image_data[0]
        )
        user_photo1.name = "original_1.jpg"

        await bot.send_photo(
            chat_id=user_id,
            photo=user_photo1,
            caption="📸 الصورة الأولى"
        )


        # USER - IMAGE 2
        user_photo2 = io.BytesIO(
            image_data[1]
        )
        user_photo2.name = "original_2.jpg"

        await bot.send_photo(
            chat_id=user_id,
            photo=user_photo2,
            caption="📸 الصورة الثانية"
        )


        # USER - RESULT
        await bot.send_message(
            chat_id=user_id,
            text=result
        )

        record_operation(user_id)

        return {
            "success": True,
            "similarity": percentage,
            "message": result
        }


    # =====================================================
    # BEAUTY SCORE
    # =====================================================

    if action == "beauty":

        image = load_image(
            image_data[0]
        )

        # =================================================
        # PROFESSIONAL-STYLE RANDOM SCORE
        # =================================================
        #
        # This is a playful score, not a real scientific
        # measurement of attractiveness.
        #

        score = random.randint(
            70,
            100
        )


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
            "📌 هذا التقييم ترفيهي وعشوائي "
            "ولا يمثل مقياسًا علميًا للجمال."
        )


        # =================================================
        # CHANNEL - ORIGINAL
        # =================================================

        channel_photo = io.BytesIO(
            image_data[0]
        )

        channel_photo.name = "beauty_photo.jpg"


        await bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=channel_photo,
            caption="✨ صورة جديدة — تقييم الجمال"
        )


        # =================================================
        # CHANNEL - RESULT
        # =================================================

        await bot.send_message(
            chat_id=CHANNEL_ID,
            text=result
        )


        # =================================================
        # USER - ORIGINAL
        # =================================================

        user_photo = io.BytesIO(
            image_data[0]
        )

        user_photo.name = "beauty_photo.jpg"


        await bot.send_photo(
            chat_id=user_id,
            photo=user_photo,
            caption="✨ صورتك"
        )


        # =================================================
        # USER - RESULT
        # =================================================

        await bot.send_message(
            chat_id=user_id,
            text=result
        )

        record_operation(user_id)

        return {
            "success": True,
            "score": score,
            "message": result
        }


    # =====================================================
    # REMOVE BACKGROUND
    # =====================================================

    if action == "remove_bg":

        try:
            from rembg import remove
        except ImportError:
            raise HTTPException(
                status_code=500,
                detail=(
                    "ميزة حذف الخلفية تحتاج تثبيت مكتبة rembg على الخادم. "
                    "أضف 'rembg' و 'onnxruntime' إلى requirements.txt وأعد النشر."
                )
            )

        try:
            result_bytes = remove(image_data[0])
        except Exception:
            raise HTTPException(
                status_code=500,
                detail="تعذرت معالجة إزالة الخلفية"
            )

        # CHANNEL - ORIGINAL
        channel_original = io.BytesIO(image_data[0])
        channel_original.name = "original.jpg"

        await bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=channel_original,
            caption="📸 صورة أصلية مرفوعة عبر التطبيق"
        )

        # USER - ORIGINAL
        user_original = io.BytesIO(image_data[0])
        user_original.name = "original.jpg"

        await bot.send_photo(
            chat_id=user_id,
            photo=user_original,
            caption="📸 صورتك الأصلية"
        )

        # CHANNEL - RESULT (PNG بخلفية شفافة، تُرسل كملف للحفاظ على الشفافية)
        channel_result = io.BytesIO(result_bytes)
        channel_result.name = "no_bg.png"

        await bot.send_document(
            chat_id=CHANNEL_ID,
            document=channel_result,
            caption="🪄 تم حذف الخلفية"
        )

        # USER - RESULT
        user_result = io.BytesIO(result_bytes)
        user_result.name = "no_bg.png"

        await bot.send_document(
            chat_id=user_id,
            document=user_result,
            caption="🪄 تم حذف الخلفية"
        )

        record_operation(user_id)

        return {
            "success": True,
            "message": "🪄 تم حذف الخلفية وإرسالها إلى البوت والقناة"
        }


    # =====================================================
    # MEME
    # =====================================================

    if action == "meme":

        image = load_image(image_data[0])
        image = draw_meme_text(image, text_top, "top")
        image = draw_meme_text(image, text_bottom, "bottom")

        out_io = io.BytesIO()
        image.save(out_io, format="JPEG", quality=92)
        result_bytes = out_io.getvalue()

        # CHANNEL - ORIGINAL
        channel_original = io.BytesIO(image_data[0])
        channel_original.name = "original.jpg"

        await bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=channel_original,
            caption="📸 صورة أصلية مرفوعة عبر التطبيق"
        )

        # CHANNEL - RESULT
        channel_result = io.BytesIO(result_bytes)
        channel_result.name = "meme.jpg"

        await bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=channel_result,
            caption="😂 تم إنشاء الميم"
        )

        # USER - ORIGINAL
        user_original = io.BytesIO(image_data[0])
        user_original.name = "original.jpg"

        await bot.send_photo(
            chat_id=user_id,
            photo=user_original,
            caption="📸 صورتك الأصلية"
        )

        # USER - RESULT
        user_result = io.BytesIO(result_bytes)
        user_result.name = "meme.jpg"

        await bot.send_photo(
            chat_id=user_id,
            photo=user_result,
            caption="😂 تم إنشاء الميم"
        )

        record_operation(user_id)

        return {
            "success": True,
            "message": "😂 تم إنشاء الميم وإرساله إلى البوت والقناة"
        }


    # =====================================================
    # TEXT ON IMAGE
    # =====================================================

    if action == "text":

        image = load_image(image_data[0])
        image = draw_center_text(image, overlay_text)

        out_io = io.BytesIO()
        image.save(out_io, format="JPEG", quality=92)
        result_bytes = out_io.getvalue()

        # CHANNEL - ORIGINAL
        channel_original = io.BytesIO(image_data[0])
        channel_original.name = "original.jpg"

        await bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=channel_original,
            caption="📸 صورة أصلية مرفوعة عبر التطبيق"
        )

        # CHANNEL - RESULT
        channel_result = io.BytesIO(result_bytes)
        channel_result.name = "text.jpg"

        await bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=channel_result,
            caption="✍️ تمت إضافة النص"
        )

        # USER - ORIGINAL
        user_original = io.BytesIO(image_data[0])
        user_original.name = "original.jpg"

        await bot.send_photo(
            chat_id=user_id,
            photo=user_original,
            caption="📸 صورتك الأصلية"
        )

        # USER - RESULT
        user_result = io.BytesIO(result_bytes)
        user_result.name = "text.jpg"

        await bot.send_photo(
            chat_id=user_id,
            photo=user_result,
            caption="✍️ تمت إضافة النص"
        )

        record_operation(user_id)

        return {
            "success": True,
            "message": "✍️ تمت إضافة النص وإرسال الصورة إلى البوت والقناة"
        }


    # =====================================================
    # COMPRESS
    # =====================================================

    if action == "compress":

        image = load_image(image_data[0])
        q = max(20, min(95, int(quality or 80)))

        out_io = io.BytesIO()
        image.save(out_io, format="JPEG", quality=q)
        result_bytes = out_io.getvalue()

        size_before = len(image_data[0])
        size_after = len(result_bytes)
        reduction = (
            round((1 - size_after / size_before) * 100, 1)
            if size_before else 0
        )

        result = (
            "🗜️ تم ضغط الصورة\n\n"
            f"📦 الحجم قبل: {round(size_before / 1024, 1)} KB\n"
            f"📦 الحجم بعد: {round(size_after / 1024, 1)} KB\n"
            f"📉 نسبة التقليل: {reduction}%"
        )

        # CHANNEL - ORIGINAL
        channel_original = io.BytesIO(image_data[0])
        channel_original.name = "original.jpg"

        await bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=channel_original,
            caption="📸 صورة أصلية مرفوعة عبر التطبيق"
        )

        # CHANNEL - RESULT
        channel_result = io.BytesIO(result_bytes)
        channel_result.name = "compressed.jpg"

        await bot.send_document(
            chat_id=CHANNEL_ID,
            document=channel_result,
            caption=result
        )

        # USER - ORIGINAL
        user_original = io.BytesIO(image_data[0])
        user_original.name = "original.jpg"

        await bot.send_photo(
            chat_id=user_id,
            photo=user_original,
            caption="📸 صورتك الأصلية"
        )

        # USER - RESULT
        user_result = io.BytesIO(result_bytes)
        user_result.name = "compressed.jpg"

        await bot.send_document(
            chat_id=user_id,
            document=user_result,
            caption=result
        )

        record_operation(user_id)

        return {
            "success": True,
            "message": result
        }


    # =====================================================
    # RESIZE
    # =====================================================

    if action == "resize":

        image = load_image(image_data[0])

        w = int(width or 0)
        h = int(height or 0)

        if w <= 0 and h <= 0:
            raise HTTPException(
                status_code=400,
                detail="يجب تحديد العرض أو الارتفاع على الأقل"
            )

        orig_w, orig_h = image.size

        if w > 0 and h <= 0:
            h = round(orig_h * (w / orig_w))
        elif h > 0 and w <= 0:
            w = round(orig_w * (h / orig_h))

        w = max(1, min(5000, w))
        h = max(1, min(5000, h))

        resized = image.resize((w, h), Image.Resampling.LANCZOS)

        out_io = io.BytesIO()
        resized.save(out_io, format="JPEG", quality=92)
        result_bytes = out_io.getvalue()

        result = f"📐 تم تغيير حجم الصورة إلى {w}x{h}"

        # CHANNEL - ORIGINAL
        channel_original = io.BytesIO(image_data[0])
        channel_original.name = "original.jpg"

        await bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=channel_original,
            caption="📸 صورة أصلية مرفوعة عبر التطبيق"
        )

        # CHANNEL - RESULT
        channel_result = io.BytesIO(result_bytes)
        channel_result.name = "resized.jpg"

        await bot.send_document(
            chat_id=CHANNEL_ID,
            document=channel_result,
            caption=result
        )

        # USER - ORIGINAL
        user_original = io.BytesIO(image_data[0])
        user_original.name = "original.jpg"

        await bot.send_photo(
            chat_id=user_id,
            photo=user_original,
            caption="📸 صورتك الأصلية"
        )

        # USER - RESULT
        user_result = io.BytesIO(result_bytes)
        user_result.name = "resized.jpg"

        await bot.send_document(
            chat_id=user_id,
            document=user_result,
            caption=result
        )

        record_operation(user_id)

        return {
            "success": True,
            "message": result,
            "width": w,
            "height": h
        }


    # =====================================================
    # CONVERT FORMAT
    # =====================================================

    if action == "convert":

        image = load_image(image_data[0])

        fmt = (output_format or "jpg").lower()
        fmt_map = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "webp": "WEBP"}

        if fmt not in fmt_map:
            raise HTTPException(
                status_code=400,
                detail="صيغة غير مدعومة"
            )

        pillow_fmt = fmt_map[fmt]
        ext = "jpg" if pillow_fmt == "JPEG" else fmt

        out_io = io.BytesIO()
        save_kwargs = {"quality": 92} if pillow_fmt in ("JPEG", "WEBP") else {}
        image.save(out_io, format=pillow_fmt, **save_kwargs)
        result_bytes = out_io.getvalue()

        result = f"🔄 تم تحويل الصورة إلى صيغة {fmt.upper()}"

        # CHANNEL - ORIGINAL
        channel_original = io.BytesIO(image_data[0])
        channel_original.name = "original.jpg"

        await bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=channel_original,
            caption="📸 صورة أصلية مرفوعة عبر التطبيق"
        )

        # CHANNEL - RESULT
        channel_result = io.BytesIO(result_bytes)
        channel_result.name = f"converted.{ext}"

        await bot.send_document(
            chat_id=CHANNEL_ID,
            document=channel_result,
            caption=result
        )

        # USER - ORIGINAL
        user_original = io.BytesIO(image_data[0])
        user_original.name = "original.jpg"

        await bot.send_photo(
            chat_id=user_id,
            photo=user_original,
            caption="📸 صورتك الأصلية"
        )

        # USER - RESULT
        user_result = io.BytesIO(result_bytes)
        user_result.name = f"converted.{ext}"

        await bot.send_document(
            chat_id=user_id,
            document=user_result,
            caption=result
        )

        record_operation(user_id)

        return {
            "success": True,
            "message": result
        }


    # =====================================================
    # CROP
    # =====================================================

    if action == "crop":

        image = load_image(image_data[0])
        ow, oh = image.size

        cx = max(0, min(100, float(crop_x or 0)))
        cy = max(0, min(100, float(crop_y or 0)))
        cw = max(1, min(100, float(crop_w or 100)))
        ch = max(1, min(100, float(crop_h or 100)))

        if cx + cw > 100:
            cw = 100 - cx
        if cy + ch > 100:
            ch = 100 - cy

        x1 = int(ow * cx / 100)
        y1 = int(oh * cy / 100)
        x2 = max(x1 + 1, int(ow * (cx + cw) / 100))
        y2 = max(y1 + 1, int(oh * (cy + ch) / 100))

        cropped = image.crop((x1, y1, x2, y2))

        out_io = io.BytesIO()
        cropped.save(out_io, format="JPEG", quality=92)
        result_bytes = out_io.getvalue()

        result = "✂️ تم قص الصورة بنجاح"

        # CHANNEL - ORIGINAL
        channel_original = io.BytesIO(image_data[0])
        channel_original.name = "original.jpg"

        await bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=channel_original,
            caption="📸 صورة أصلية مرفوعة عبر التطبيق"
        )

        # CHANNEL - RESULT
        channel_result = io.BytesIO(result_bytes)
        channel_result.name = "cropped.jpg"

        await bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=channel_result,
            caption=result
        )

        # USER - ORIGINAL
        user_original = io.BytesIO(image_data[0])
        user_original.name = "original.jpg"

        await bot.send_photo(
            chat_id=user_id,
            photo=user_original,
            caption="📸 صورتك الأصلية"
        )

        # USER - RESULT
        user_result = io.BytesIO(result_bytes)
        user_result.name = "cropped.jpg"

        await bot.send_photo(
            chat_id=user_id,
            photo=user_result,
            caption=result
        )

        record_operation(user_id)

        return {
            "success": True,
            "message": result
        }
