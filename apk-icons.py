#!/usr/bin/env python3
"""Android projesindeki varsayilan Capacitor simgelerini ve acilis ekranini
HaberAkis simgesiyle degistirir. Kullanim: python apk-icons.py <android/app/src/main/res>"""
import os
import re
import sys

from PIL import Image, ImageDraw

res = sys.argv[1]
BG = (15, 17, 23)
icon = Image.open("icon-512.png").convert("RGBA")
maskable = Image.open("icon-maskable-512.png").convert("RGBA")

legacy = {"mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192}
foreground = {"mdpi": 108, "hdpi": 162, "xhdpi": 216, "xxhdpi": 324, "xxxhdpi": 432}

for dens, size in legacy.items():
    folder = os.path.join(res, f"mipmap-{dens}")
    if not os.path.isdir(folder):
        continue
    square = icon.resize((size, size), Image.LANCZOS)
    square.save(os.path.join(folder, "ic_launcher.png"))
    mask = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size * 4 - 1, size * 4 - 1), fill=255)
    mask = mask.resize((size, size), Image.LANCZOS)
    round_icon = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    round_icon.paste(square, (0, 0), mask)
    round_icon.save(os.path.join(folder, "ic_launcher_round.png"))
    fsize = foreground[dens]
    maskable.resize((fsize, fsize), Image.LANCZOS).save(os.path.join(folder, "ic_launcher_foreground.png"))

# Adaptif simge arka plan rengi
bg_xml = os.path.join(res, "values", "ic_launcher_background.xml")
if os.path.exists(bg_xml):
    text = open(bg_xml, encoding="utf-8").read()
    open(bg_xml, "w", encoding="utf-8").write(re.sub(r"#[0-9A-Fa-f]{6}", "#0F1117", text))

# Durum/gezinme cubugu rengi (koyu) ve beyaz simgeler
styles = os.path.join(res, "values", "styles.xml")
if os.path.exists(styles):
    text = open(styles, encoding="utf-8").read()
    extra = (
        '        <item name="android:statusBarColor">#0F1117</item>\n'
        '        <item name="android:navigationBarColor">#0F1117</item>\n'
        '        <item name="android:windowLightStatusBar">false</item>\n'
        '        <item name="android:windowLightNavigationBar">false</item>\n'
    )
    marker = '<item name="android:background">@null</item>\n'
    if marker in text and "statusBarColor" not in text:
        text = text.replace(marker, marker + extra, 1)
        open(styles, "w", encoding="utf-8").write(text)

# Acilis ekrani: koyu zemin + ortada simge
count = 0
for root, _, files in os.walk(res):
    for name in files:
        if name == "splash.png":
            path = os.path.join(root, name)
            w, h = Image.open(path).size
            canvas = Image.new("RGB", (w, h), BG)
            side = int(min(w, h) * 0.28)
            logo = icon.resize((side, side), Image.LANCZOS)
            canvas.paste(logo, ((w - side) // 2, (h - side) // 2), logo)
            canvas.save(path)
            count += 1
print(f"Simgeler guncellendi, {count} acilis ekrani yenilendi.")
