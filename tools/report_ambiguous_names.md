# Ручная проверка ambiguous ников (OCR-assisted reference)

Эталон `ocr_nicknames_reference.json` получен через OCR/bbox,
а не вручную по изображениям. Записи ниже помечены ambiguous —
проверьте их по скриншотам в `C:\Users\2006k\Downloads\скрины тест`
и исправьте `nickname`/`secondary_text`/`ambiguous` в JSON при
расхождении. После правки пересчитайте `tools/eval_nicknames.py`.

| # | Файл | Сторона | Строка | OCR nickname | OCR secondary | Эталон nickname | Эталон secondary |
|---|------|---------|--------|--------------|---------------|-----------------|-----------------|
| 1 | `2026-11-11_203103.png` | left | 2 | rkz Aikyn | — | rkzAikyn | — |
| 2 | `2026-11-11_203103.png` | left | 3 | DX  Karim | — | DXKarim | — |
| 3 | `2026-11-11_203103.png` | right | 3 | god Nurbo  me | — | god Nurbo me | — |
| 4 | `i1ge0.jpg` | left | 1 | 9'CONQUEST | N1GHTERR | 9CONQUEST | N1GHTERR |
| 5 | `i1ge0.jpg` | left | 3 | D1NoSkill | — | D1NoSkill | — |
| 6 | `i1ge0.jpg` | left | 4 | DXKarim | Dynastyx | DXKarim | Dynastyx |
| 7 | `i1ge0.jpg` | right | 3 | god Nurbome | — | god Nurbome | — |
| 8 | `i1ge0.jpg` | right | 4 | NRJARONN当 | — | TNRJARONN当 | — |
| 9 | `im1age.png` | right | 2 | good maku | — | good maku me | — |
| 10 | `im1age.png` | right | 3 | god Nurbo me | — | god Nurbo me | — |
| 11 | `image0.jpg` | left | 1 | 9CONQUEST | GHTERRAY | 9CONQUEST | GHTERRAY |
| 12 | `image0.jpg` | left | 3 | god Nurbome | — | god Nurbo me | — |
| 13 | `image0.jpg` | left | 4 | nurss | — | nurss444 | — |
| 14 | `image0.jpg` | right | 1 | BL KAISER | — | BL KAISER | — |
| 15 | `image1.png` | left | 1 | PRAIM BAKO | — | PRAIMBAKO | — |
| 16 | `image1.png` | left | 2 | uAluaR!z3 | 00 | uAluaR!z3 | r!z3 |
| 17 | `image1.png` | right | 1 | DSDOYLE | BUBBLE GUM | DSDOYLE | BUBBLEGUM |
| 18 | `image1.png` | right | 2 | 5q dauren4ik | — | bs dauren4ik | — |
| 19 | `image1.png` | right | 3 | uluking | 1337 | uluking 1337 | 1337 |
| 20 | `image1.png` | right | 4 | CaHxKapA47361 | — | CaHxKapA47361 | — |
| 21 | `IMG_1459.png` | left | 4 | 91CONQUEST | GHTERRA | 91CONQUEST | GHTERRA |
| 22 | `IMG_1459.png` | right | 4 | DX Karim | Dyhastyx | DXKarim | Dynastyx |
| 23 | `IMG_9580.png` | left | 4 | 91CONQUEST | IGHTERR | 911CONQUEST | NIGHTERR |
| 24 | `IMG_9580.png` | right | 3 | DX Karim | Dyhastyx | DX Karim | Dynastyx |
| 25 | `IMG_9694.png` | left | 2 | rkz Aikyn | — | rkz Aikyn | — |
| 26 | `IMG_9694.png` | left | 4 | rNRg ARONNI | — | TNR ARONNI | — |
| 27 | `IMG_9694.png` | right | 1 | DIAT | — | UKDIAT | — |
| 28 | `IMG_9694.png` | right | 3 | ZERO   AGAIN | dalbaebsigem | ZEROAGAIN | dalbaebsigem |
| 29 | `match_result.png` | left | 2 | pee kma-x | — | peekma-xi | — |
| 30 | `match_result.png` | right | 1 | mowar | — | Imowa13 | r!z3 |
| 31 | `match_result.png` | right | 2 | 7X Francesco | F4.BAUIRLAR | 7X Francesco | F4-BAUIRLAR |
| 32 | `match_result.png` | right | 3 | gek4t44dope | SS DIANI | gek4t44dope | SS DIANI |
| 33 | `Screenshot_20261111_222102_com_dts_freefireth_FFMainActivity.jpg` | left | 2 | isane | — | isa | — |
| 34 | `Screenshot_20261111_222102_com_dts_freefireth_FFMainActivity.jpg` | left | 4 | GOAT Karma | GOATS | GOAT Karma | GOATS |
| 35 | `Screenshot_20261111_222102_com_dts_freefireth_FFMainActivity.jpg` | right | 2 | HACBAN | Qazyna | 17 HACBAN | Qazyna |
| 36 | `Screenshot_20261111_222102_com_dts_freefireth_FFMainActivity.jpg` | right | 3 | as apple16 | NZT48 | asapple16 | NZT48 |
| 37 | `Screenshot_20261111_222102_com_dts_freefireth_FFMainActivity.jpg` | right | 4 | GOAT ANFAH | GOATS | GOAT ANFAH | GOATS |

Всего ambiguous: 37 (missing fast data: 0).
