"""Reproduce the application's original vector diamond as a Windows icon.

Requires Pillow. Run from any directory. ICO contains 16 through 256 px frames.
"""
from pathlib import Path
from PIL import Image, ImageDraw

out = Path(__file__).resolve().parents[1] / 'src/IW3MapPorter.Desktop/Assets'
out.mkdir(parents=True, exist_ok=True)
scale = 4
im = Image.new('RGBA', (256 * scale, 256 * scale))
draw = ImageDraw.Draw(im)
def xy(points): return [(round(x * scale), round(y * scale)) for x, y in points]
draw.rounded_rectangle((0, 0, 256 * scale - 1, 256 * scale - 1), radius=67 * scale, fill='#172A24')
# Same 2:1 outer/inner diamond proportions as the original WPF path.
draw.polygon(xy([(128,34),(222,128),(128,222),(34,128)]), fill='#67D9B5')
draw.polygon(xy([(128,81),(175,128),(128,175),(81,128)]), fill='#172A24')
im = im.resize((256,256), Image.Resampling.LANCZOS)
im.save(out/'IW3MapPorter.ico', sizes=[(s,s) for s in (16,20,24,32,40,48,64,128,256)])
if __name__ == '__main__': print(out/'IW3MapPorter.ico')
