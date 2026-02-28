"""One-shot PWA icon generator for PC Deal Finder.
Run once: pip install Pillow && python make_icons.py
The generated files are committed to git; Pillow is not needed at runtime.
"""
from PIL import Image, ImageDraw
import os

BG = (8,  12, 16)    # --bg:    #080c10
FG = (0, 229, 160)   # --green: #00e5a0

os.makedirs('static/icons', exist_ok=True)


def make_icon(size):
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    d   = ImageDraw.Draw(img)
    r   = max(size // 8, 4)
    # Rounded-square dark background
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=r, fill=BG)
    pad   = size // 6
    bar_h = max(size // 8, 2)
    gap   = max(size // 12, 1)
    # Top bar — full width (represents higher market price)
    d.rounded_rectangle(
        [pad,        size // 3 - bar_h // 2 - gap // 2,
         size - pad, size // 3 + bar_h // 2 - gap // 2],
        radius=max(bar_h // 4, 1), fill=FG,
    )
    # Bottom bar — shorter (represents the deal price below market)
    d.rounded_rectangle(
        [pad,            size * 2 // 3 - bar_h // 2 + gap // 2,
         size - pad * 2, size * 2 // 3 + bar_h // 2 + gap // 2],
        radius=max(bar_h // 4, 1), fill=FG,
    )
    return img


# Manifest icons
for size, name in [
    (192, 'icon-192.png'),
    (512, 'icon-512.png'),
    (180, 'apple-touch-icon.png'),
]:
    make_icon(size).save(f'static/icons/{name}')
    print(f'  static/icons/{name}')

# Favicon source PNGs
for s in [32, 16]:
    make_icon(s).save(f'static/icons/favicon-{s}.png')
    print(f'  static/icons/favicon-{s}.png')

# Multi-size ICO
imgs = [
    Image.open(f'static/icons/favicon-{s}.png').convert('RGBA')
    for s in [32, 16]
]
imgs[0].save('static/favicon.ico', format='ICO', append_images=imgs[1:])
print('  static/favicon.ico')
print('Done.')
