"""Ink projection по Y в колонке ИМЯ — сколько текстовых линий в строке."""
import sys
sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from pathlib import Path
import numpy as np, cv2

p = Path(r"C:\Users\2006k\Downloads\скрины тест\image0.jpg")
img = cv2.imdecode(np.frombuffer(p.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
# left name col x 519-728 (из geometry), rows y 540-980
x0,x1 = 500, 740
for y0,y1,tag in [(540,670,"row1"),(660,780,"row2"),(765,880,"row3"),(870,985,"row4")]:
    strip = gray[y0:y1, x0:x1]
    # текст светлый на тёмном — берём яркие пиксели
    mask = strip > 140
    proj = mask.sum(axis=1)
    # печатаем профиль
    line = ""
    for i,v in enumerate(proj):
        line += "#" if v>15 else ("+" if v>4 else ".")
    print(f"{tag} y{y0}-{y1}")
    print("   ", line)
