import os
import io
import logging
from PIL import Image, ImageOps
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from telegram import Bot

logging.basicConfig(level=logging.INFO)

app = FastAPI()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not configured")

bot = Bot(token=BOT_TOKEN)


@app.get("/")
async def home():
    return {
        "status": "online",
        "message": "Telegram Image Server is running"
    }


def process_image(data: bytes):
    try:
        image = Image.open(io.BytesIO(data))
        image = ImageOps.exif_transpose(image)
        image = image.convert("RGB")
        return image
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Invalid image"
        )


@app.post("/process")
async def process(
    action: str = Form(...),
    images: list[UploadFile] = File(...)
):

    if action not in ["sticker", "compare"]:
        raise HTTPException(
            status_code=400,
            detail="Invalid action"
        )

    if action == "sticker" and len(images) != 1:
        raise HTTPException(
            status_code=400,
            detail="Sticker requires one image"
        )

    if action == "compare" and len(images) != 2:
        raise HTTPException(
            status_code=400,
            detail="Comparison requires two images"
        )

    processed_images = []

    for upload in images:
        data = await upload.read()

        if len(data) > 10 * 1024 * 1024:
            raise HTTPException(
                status_code=400,
                detail="Image is too large"
            )

        image = process_image(data)
        processed_images.append(image)

    # =========================
    # STICKER
    # =========================

    if action == "sticker":

        image = processed_images[0]

        max_size = 512
        image.thumbnail(
            (max_size, max_size),
            Image.Resampling.LANCZOS
        )

        sticker = io.BytesIO()
        image.save(
            sticker,
            format="WEBP",
            quality=90
        )
        sticker.seek(0)
        sticker.name = "sticker.webp"

        await bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=io.BytesIO(
                images[0].file.read()
                if False else b""
            )
        )

        return {
            "success": True,
            "message": "Sticker processed successfully"
        }


    # =========================
    # COMPARE
    # =========================

    if action == "compare":

        img1 = processed_images[0]
        img2 = processed_images[1]

        # توحيد الحجم للمقارنة
        size = (300, 300)

        img1 = ImageOps.fit(img1, size)
        img2 = ImageOps.fit(img2, size)

        # حساب فرق بسيط بين الصورتين
        import numpy as np

        arr1 = np.array(img1).astype(float)
        arr2 = np.array(img2).astype(float)

        difference = np.mean(
            np.abs(arr1 - arr2)
        )

        similarity = max(
            0,
            min(
                100,
                100 - (difference / 255 * 100)
            )
        )

        return {
            "success": True,
            "similarity": round(similarity, 2)
        }
