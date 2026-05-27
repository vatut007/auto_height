#!/usr/bin/env python3
"""
auto_tune.py — автоподбор overrides для LaTeX документа.

Использует:
- document.aux — координаты строк таблиц (zsavepos метки)
- document.pdf — реальная нижняя граница контента на каждой странице
- request.json — overrides для обновления

Алгоритм:
1. Из .aux берём строки каждой таблицы и их страницы
2. Из PDF измеряем реальную нижнюю границу контента на каждой странице
3. Пустое место = рабочая граница страницы - нижняя граница контента
4. Если пустое место > порога — распределяем его по строкам таблицы на странице

Зависимости:
    pip install pdfplumber
"""

import re
import json
import shutil
from pathlib import Path
from copy import deepcopy

try:
    import pdfplumber
except ImportError:
    raise SystemExit("Установи зависимость: pip install pdfplumber")

HERE      = Path(__file__).parent
AUX_PATH  = HERE / "document.aux"
PDF_PATH  = HERE / "document.pdf"
JSON_PATH = HERE / "request.json"

# Нижняя граница рабочей области страницы в pdfplumber координатах (pt от верха)
# Всё ниже этой отметки — штамп (рамка, подписи, номер листа)
STAMP_TOP_PT = 780.0

# Минимальное пустое место которое считаем проблемой (pt)
MIN_EMPTY_PT = 30

# Доля пустого места которую заполняем за одну итерацию
# 0.3 = медленный рост, строки не перескакивают на следующую страницу
FILL_RATIO = 0.3

# Для технологических карт — более агрессивное заполнение
# Строки там заведомо большие и пустое место нужно убирать быстрее
FILL_RATIO_TECHCARD = 0.85

# Если одна строка на странице и пустого места больше этого — пропуск
# (строка уехала на новую страницу одна)
ORPHAN_ROW_THRESHOLD_PT = 400

# Конвертация LaTeX sp → pt
SP_PER_PT = 65536


# ══════════════════════════════════════════════════════════════════════════════
# Парсинг .aux
# ══════════════════════════════════════════════════════════════════════════════

def parse_aux(aux_path: Path) -> tuple[dict, set[int]]:
    """
    Читает .aux и возвращает:
    1. labels - словарь всех zsavepos меток
    2. section_end_pages - страницы которые являются концом раздела
       (следующая страница начинается с section - пробел там допустим)
    """
    text = aux_path.read_text(encoding="utf-8", errors="ignore")

    # Страницы где начинается новый \section
    section_start_pages = set()
    for m in re.finditer(
        r'\\newlabel\{sec:[^}]*\}\{\{[^}]*\}\{(\d+)\}[^\\]*section\.\d',
        text
    ):
        section_start_pages.add(int(m.group(1)))

    # Конец раздела = страница перед началом следующего раздела
    section_end_pages = {p - 1 for p in section_start_pages if p > 1}

    # Позиции переходов страниц
    page_breaks = sorted({
        m.start()
        for m in re.finditer(r'\\pgfsyspdfmark \{pgfid\d+\}\{\d+\}\{\d+\}', text)
    })

    def get_page(file_pos: int) -> int:
        return sum(1 for pb in page_breaks if pb <= file_pos)

    labels = {}
    for m in re.finditer(
        r'\\zref@newlabel\{([^}]+)\}\{\\posx\{(\d+)\}\\posy\{(\d+)\}\}',
        text
    ):
        labels[m.group(1)] = {
            "posx":    int(m.group(2)),
            "posy":    int(m.group(3)),
            "abspage": get_page(m.start()),
        }

    return labels, section_end_pages


# ══════════════════════════════════════════════════════════════════════════════
# Измерение нижней границы контента через pdfplumber
# ══════════════════════════════════════════════════════════════════════════════

def get_content_bottom(page) -> float | None:
    """
    Возвращает нижнюю границу контента на странице (pt от верха).
    Исключает рамку страницы и штамп внизу.
    """
    page_h = page.height
    page_w = page.width
    objects = []

    # Символы текста
    for c in page.chars:
        if c['bottom'] < STAMP_TOP_PT:
            objects.append(c['bottom'])

    # Линии — не рамка страницы, не штамп
    for l in page.lines:
        w = abs(l['x1'] - l['x0'])
        h = abs(l['y1'] - l['y0'])
        if w > page_w * 0.85 or h > page_h * 0.85:
            continue
        if l['bottom'] >= STAMP_TOP_PT:
            continue
        objects.append(l['bottom'])

    # Прямоугольники — не рамка страницы, не штамп
    for r in page.rects:
        if r['width'] > page_w * 0.85 and r['height'] > page_h * 0.85:
            continue
        if r['bottom'] >= STAMP_TOP_PT:
            continue
        objects.append(r['bottom'])

    # Изображения
    for img in page.images:
        if img['bottom'] >= STAMP_TOP_PT:
            continue
        objects.append(img['bottom'])

    return max(objects) if objects else None


def build_page_bottoms(pdf_path: Path) -> dict[int, float]:
    """
    Возвращает {page_number: content_bottom_pt} для всех страниц.
    page_number — 1-based.
    """
    result = {}
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            cb = get_content_bottom(page)
            if cb is not None:
                result[i + 1] = cb
    return result


# ══════════════════════════════════════════════════════════════════════════════
# Анализ таблиц
# ══════════════════════════════════════════════════════════════════════════════

def calc_add_proportional(
    table_data: dict,
    row_indices: list[int],
    empty_pt: int,
    row_texts: dict[int, int] | None = None,
    fill_ratio: float = FILL_RATIO,
    last_row_takes_all: bool = False,
    last_row_idx: int | None = None,
) -> dict[str, int]:
    """
    Распределяет empty_pt между строками.

    Если last_row_takes_all=True — строка last_row_idx получает всё пустое место.
    Если last_row_idx не задан — используется последний элемент row_indices.
    """
    fill_pt = round(empty_pt * fill_ratio)

    if last_row_takes_all:
        target = last_row_idx if last_row_idx is not None else row_indices[-1]
        result = {str(i): 0 for i in row_indices}
        result[str(target)] = fill_pt
        return result

    if not row_texts:
        add = max(10, fill_pt // len(row_indices))
        return {str(i): add for i in row_indices}

    # Суммарная длина текста
    lengths = {i: row_texts.get(i, 30) for i in row_indices}
    total   = sum(lengths.values()) or 1

    result      = {}
    distributed = 0
    indices     = list(row_indices)

    for j, idx in enumerate(indices):
        if j == len(indices) - 1:
            result[str(idx)] = max(10, fill_pt - distributed)
        else:
            add = max(10, round(fill_pt * lengths[idx] / total))
            result[str(idx)] = add
            distributed += add

    return result


def analyze_table(
    labels: dict,
    page_bottoms: dict[int, float],
    table_name: str,
    page_height_pt: float,
    section_end_pages: set[int],
) -> list[dict]:
    """
    Для каждой страницы где есть строки таблицы:
    - берём реальную нижнюю границу контента из PDF
    - считаем пустое место
    - если > порога — добавляем строкам высоту

    Возвращает список проблемных страниц:
    [{"page": N, "empty_pt": X, "rows": [...], "add_per_row": {...}}, ...]
    """
    safe = table_name.replace(" ", "_").replace(":", "-")

    # Собираем строки таблицы
    rows = {}
    idx = 0
    while True:
        s_key = f"row:s:{safe}:{idx}"
        e_key = f"row:e:{safe}:{idx}"
        if s_key not in labels:
            break
        start_pdf = page_height_pt - labels[s_key]["posy"] / SP_PER_PT
        end_pdf   = page_height_pt - labels[e_key]["posy"] / SP_PER_PT \
                    if e_key in labels else start_pdf
        start_page = labels[s_key]["abspage"]
        end_page   = labels[e_key]["abspage"] if e_key in labels else start_page

        rows[idx] = {
            "start_page": start_page,
            "end_page":   end_page,
            "start_pdf":  start_pdf,
            "end_pdf":    end_pdf,
            # Строка "на странице N" если она там начинается И там же заканчивается
            # Если строка переходит через страницу — относим к странице где заканчивается
            "page": end_page,
        }
        idx += 1

    if not rows:
        return []

    # Группируем по странице ОКОНЧАНИЯ строки
    pages: dict[int, list[int]] = {}
    for row_idx, info in rows.items():
        pages.setdefault(info["page"], []).append(row_idx)

    result = []

    for page, row_indices in sorted(pages.items()):
        # Реальная нижняя граница контента на этой странице (из PDF)
        content_bottom = page_bottoms.get(page)
        if content_bottom is None:
            continue

        empty_pt = STAMP_TOP_PT - content_bottom

        if empty_pt < MIN_EMPTY_PT:
            continue

        # Конец раздела — пробел допустим, пропускаем
        if page in section_end_pages:
            print(f"    стр. {page}: конец раздела — пропуск")
            continue

        # Одна строка-одиночка → пропуск
        if len(row_indices) == 1 and empty_pt > ORPHAN_ROW_THRESHOLD_PT:
            print(f"    стр. {page}: строка {row_indices[0]} одна — пропуск")
            continue

        # Находим реально последнюю строку на странице —
        # ту у которой end_pdf максимальный (самая нижняя)
        # Строки которые только начались на этой странице но уходят на следующую
        # — не трогаем, их end_pdf будет на следующей странице
        rows_ending_here = [
            i for i in row_indices
            if rows[i]["end_page"] == page
        ]

        if not rows_ending_here:
            continue

        # Последняя строка = та что заканчивается ниже всех на этой странице
        last_row = max(rows_ending_here, key=lambda i: rows[i]["end_pdf"])

        fill_pt     = round(empty_pt * FILL_RATIO)
        add_per_row = max(10, fill_pt // len(rows_ending_here))

        result.append({
            "page":          page,
            "empty_pt":      round(empty_pt, 1),
            "rows":          rows_ending_here,
            "last_row":      last_row,
            "add_per_row":   {str(i): add_per_row for i in rows_ending_here},
        })

    return result


# ══════════════════════════════════════════════════════════════════════════════
# Применение патчей
# ══════════════════════════════════════════════════════════════════════════════

def patch_overrides(
    overrides: dict,
    labels: dict,
    page_bottoms: dict[int, float],
    page_height_pt: float,
    section_end_pages: set[int],
    doc: dict,
) -> bool:
    changed = False

    # ── Обычные таблицы ──────────────────────────────────────────────────────
    for table_name, table_data in overrides.get("tables", {}).items():
        problems = analyze_table(
            labels, page_bottoms, table_name,
            page_height_pt, section_end_pages,
        )
        if not problems:
            continue

        print(f"\n  '{table_name}':")
        for p in problems:
            add = list(p["add_per_row"].values())[0]
            print(f"    стр. {p['page']}: пустое {p['empty_pt']}pt  "
                  f"строки {p['rows']}  +{add}pt каждой")

            for row_key, add_pt in p["add_per_row"].items():
                if row_key not in table_data:
                    continue
                current = table_data[row_key]
                if isinstance(current, str):
                    current = int(current) if current.isdigit() else 0
                table_data[row_key] = int(current) + add_pt
                changed = True

    # ── Технологические карты ─────────────────────────────────────────────────
    TECHCARD_AUX_NAMES = ["Технологическая_карта", "Операционная_карта"]

    techcards = overrides.get("techcards", {})

    # Строим карту длин текста операций из tech_card
    # operations[i] → суммарная длина текста всех ячеек строки i
    tc_text_lengths: dict[str, dict[int, int]] = {}
    try:
        operations = (
            doc.get("content", {})
               .get("initial_data", {})
               .get("org_section", {})
               .get("tech_card", {})
               .get("operations", [])
        )
        for i, op in enumerate(operations):
            total_len = sum(
                len(str(v)) for v in [
                    op.get("name", ""),
                    op.get("place", ""),
                    op.get("tools", "") or "",
                    op.get("requirements", "") or "",
                ]
            )
            # Ключ — название работы из первого techcard
            if tc_text_lengths:
                list(tc_text_lengths.values())[0][i] = total_len
            else:
                tc_text_lengths["_first"] = {i: total_len}
    except Exception:
        pass

    first_tc_lengths = list(tc_text_lengths.values())[0] if tc_text_lengths else {}

    if techcards:
        tc_keys = list(techcards.keys())

        for i, aux_name in enumerate(TECHCARD_AUX_NAMES):
            if i >= len(tc_keys):
                break

            tc_key    = tc_keys[i]
            table_data = techcards[tc_key]
            lengths    = first_tc_lengths if i == 0 else {}

            problems = analyze_table(
                labels, page_bottoms, aux_name,
                page_height_pt, section_end_pages,
            )
            if not problems:
                continue

            print(f"\n  '{tc_key}' (aux: {aux_name}):")
            for p in problems:
                add_per_row = calc_add_proportional(
                    table_data, p["rows"], p["empty_pt"],
                    lengths,
                    fill_ratio=FILL_RATIO_TECHCARD,
                    last_row_takes_all=True,
                    last_row_idx=p.get("last_row"),
                )
                last = p.get("last_row")
                adds = [v for v in add_per_row.values() if v > 0]
                print(f"    стр. {p['page']}: пустое {p['empty_pt']}pt  "
                      f"строки {p['rows']}  "
                      f"последняя строка [{last}] +{add_per_row.get(str(last), 0)}pt")

                for row_key, add_pt in add_per_row.items():
                    if row_key not in table_data or add_pt == 0:
                        continue
                    current = table_data[row_key]
                    if isinstance(current, str):
                        current = int(current) if current.isdigit() else 0
                    # Для последней строки — устанавливаем абсолютное значение
                    # (текущий height + пустое место)
                    table_data[row_key] = int(current) + add_pt
                    changed = True

    return changed


# ══════════════════════════════════════════════════════════════════════════════
# Главная функция
# ══════════════════════════════════════════════════════════════════════════════

def main():
    for p in (AUX_PATH, PDF_PATH, JSON_PATH):
        if not p.exists():
            raise FileNotFoundError(
                f"Файл не найден: {p}\n"
                "Положи document.aux, document.pdf и request.json рядом со скриптом."
            )

    print("=" * 60)
    print("Парсинг document.aux...")
    labels, section_end_pages = parse_aux(AUX_PATH)
    total = sum(1 for k in labels if k.startswith(("table:", "row:")))
    print(f"  zsavepos меток: {total}")
    print(f"  Концы разделов на страницах: {sorted(section_end_pages)}")

    print()
    print("Анализ document.pdf...")
    with pdfplumber.open(PDF_PATH) as pdf:
        page_height_pt = pdf.pages[0].height
        page_bottoms   = {}
        for i, page in enumerate(pdf.pages):
            cb = get_content_bottom(page)
            if cb is not None:
                page_bottoms[i + 1] = cb
    print(f"  Страниц: {len(page_bottoms)}")
    print(f"  Высота страницы: {page_height_pt:.1f}pt")

    print()
    print("Читаем request.json...")
    doc       = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    overrides = deepcopy(doc.get("overrides", {}))

    print()
    print("Анализ пустого места:")
    changed = patch_overrides(
        overrides, labels, page_bottoms,
        page_height_pt, section_end_pages, doc,
    )

    print()
    if not changed:
        print("✓ Пустого места не найдено — overrides не изменены.")
        return

    backup = JSON_PATH.with_suffix(".backup.json")
    shutil.copy(JSON_PATH, backup)

    doc["overrides"] = overrides
    JSON_PATH.write_text(
        json.dumps(doc, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("=" * 60)
    print(f"✓ request.json обновлён  (резерв: {backup.name})")
    print()
    print("Следующий шаг:")
    print("  1. Скопируй overrides из request.json в API")
    print("  2. Сгенерируй документ → скомпилируй → проверь PDF")
    print("  3. Если пустое место осталось — запусти скрипт снова")


if __name__ == "__main__":
    main()