import sys
sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from pathlib import Path
import numpy as np, cv2
from services import fast_cs_ocr as F
from services.ocr_service import warmup, _get_ocr
warmup()
p = Path(r"C:\Users\2006k\Downloads\скрины тест\image0.jpg")
img = cv2.imdecode(np.frombuffer(p.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
det = F._detect_boxes(img)
_ip,_db,orig = det
boxes=[F._to_box_dict(orig[i],i) for i in range(len(orig))]
eng=_get_ocr()
# idx17,18 из прошлого вывода
jobs=[(17,boxes[17]),(18,boxes[18])]
res = F._split_merged_name_boxes(eng, img, jobs)
for k,v in res.items():
    print("box",k,[(round(i['cy']),i['text'],round(i['score'],2)) for i in v])
# также rec каждого бокса целиком для сравнения
for i,b in jobs:
    x0,y0,x1,y1=int(b['x0']),int(b['y0']),int(b['x1']),int(b['y1'])
    crop=np.ascontiguousarray(img[y0:y1,x0:x1])
    r,_=eng.text_rec(F._cap_crop_ratios([crop]))
    print("whole",i, r[0] if r else None, "h=",y1-y0)
    cv2.imwrite(f"C:/Users/2006k/Downloads/_box{i}.png", crop)
