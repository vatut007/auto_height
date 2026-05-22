#!/usr/bin/env python3
"""
latex_fill_gaps.py
==================
Автоматическое заполнение пробелов в ltablex/longtable таблицах LaTeX.

Шаг 1 (prepare):  вставляет zsavepos-метки в .tex → компилируй → получи .aux
Шаг 2 (apply):    читает .aux, считает пробелы, вставляет \\[Xpt] в .tex
Шаг 3 (inspect):  читает .aux, возвращает JSON с текущим состоянием таблиц

Использование:
    python latex_fill_gaps.py prepare  input.tex  output.tex
    # → скомпилируй output.tex → получи output.aux
    python latex_fill_gaps.py apply    input.tex  output.aux  result.tex  [--overhead 5.4]
    python latex_fill_gaps.py inspect  output.aux  [--json state.json]

JSON формат (inspect):
    {
      "tables": {
        "Операционная карта": {
          "head": 40,        // высота шапки endhead в pt
          "pages": {
            "4": { "rows": [1], "gap_pt": 61.1, "stretchable": [] },
            "5": { "rows": [2,3], "gap_pt": 187.6, "stretchable": [2] }
          },
          "rows": {
            "1": { "page": 4, "h_pt": 185.4, "add_pt": 0,   "gap_pt": 61.1 },
            "2": { "page": 5, "h_pt": 221.4, "add_pt": 404, "gap_pt": 0.6  }
          }
        }
      }
    }
"""

import re
import sys
import json
import argparse
from pathlib import Path


# ─────────────────────────────────────────────
# Константы
# ─────────────────────────────────────────────
SP       = 65536   # scaled points → pt
OVERHEAD = 5.40    # толщина \cline + внутренние отступы ltablex


# ─────────────────────────────────────────────
# Парсинг .aux
# ─────────────────────────────────────────────

def parse_aux(aux_path: str) -> dict:
    labels = {}
    pattern = re.compile(
        r'\\zref@newlabel\{([^}]+)\}\{\\posx\{(\d+)\}\\posy\{(\d+)\}\\abspage\{(\d+)\}'
    )
    with open(aux_path, 'r', encoding='utf-8') as f:
        for line in f:
            m = pattern.search(line)
            if m:
                labels[m.group(1)] = {
                    'x':    int(m.group(2)) / SP,
                    'y':    int(m.group(3)) / SP,
                    'page': int(m.group(4)),
                }
    return labels


def get_bottom(aux_path: str) -> float:
    """Нижняя граница текстовой области в координатах posy (pt)."""
    with open(aux_path, 'r', encoding='utf-8') as f:
        content = f.read()

    ph = re.search(r'\\zref@newlabel\{zref@pdfpaperheight\}\{\\the\{(\d+)\}\}', content)
    th = re.search(r'\\zref@newlabel\{zref@pdftextheight\}\{\\the\{(\d+)\}\}',  content)
    vo = re.search(r'\\zref@newlabel\{zref@pdfvorigin\}\{\\the\{(-?\d+)\}\}',   content)
    tm = re.search(r'\\zref@newlabel\{zref@pdftopmargin\}\{\\the\{(-?\d+)\}\}', content)
    hh = re.search(r'\\zref@newlabel\{zref@pdfheadheight\}\{\\the\{(-?\d+)\}\}',content)
    hs = re.search(r'\\zref@newlabel\{zref@pdfheadsep\}\{\\the\{(-?\d+)\}\}',   content)

    if ph and th and vo and tm and hh:
        paperheight = int(ph.group(1)) / SP
        textheight  = int(th.group(1)) / SP
        pdfvorigin  = int(vo.group(1)) / SP
        topmargin   = int(tm.group(1)) / SP
        headheight  = int(hh.group(1)) / SP
        headsep     = int(hs.group(1)) / SP if hs else 0.0
        bottom = paperheight - (pdfvorigin + topmargin + headheight + headsep + textheight)
        print(f"[geometry] bottom={bottom:.2f}pt (из .aux)")
        return bottom

    print(f"[geometry] zref-геометрия не найдена, используем bottom=85.36pt")
    return 85.36


# ─────────────────────────────────────────────
# Парсинг .tex — извлекаем метаданные таблиц
# ─────────────────────────────────────────────

def parse_tables_from_tex(tex_path: str) -> list:
    """
    Возвращает список таблиц найденных в .tex:
    [{ 'caption': str, 'tab_id': int, 'total_rows': int }]
    Поддерживает несколько таблиц в документе.
    """
    tables = []
    with open(tex_path, 'r', encoding='utf-8') as f:
        content = f.read()

    # Ищем все tabularx блоки с caption
    # caption идёт в начале tabularx (ltablex стиль)
    tab_pattern = re.compile(
        r'\\begin\{tabularx\}.*?\\caption\{([^}]+)\}.*?\\end\{tabularx\}',
        re.DOTALL
    )

    tab_id = 1
    for m in tab_pattern.finditer(content):
        caption = m.group(1).strip()
        # Считаем строки данных в этом блоке
        block = m.group(0)
        # Строки данных начинаются с \multicolumn{1}{|c|}{\zsavepos{row-N-top}}&N&
        row_tops = re.findall(r'\\zsavepos\{row-(\d+)-top\}', block)
        total = max(int(x) for x in row_tops) if row_tops else 0
        tables.append({
            'caption':    caption,
            'tab_id':     tab_id,
            'total_rows': total,
        })
        tab_id += 1

    return tables


# ─────────────────────────────────────────────
# Построение состояния таблицы
# ─────────────────────────────────────────────

def build_table_state(labels: dict, tab_id: int, total_rows: int,
                      bottom: float, overhead: float) -> dict:
    """
    Строит полное состояние одной таблицы из меток .aux.
    Возвращает словарь для JSON.
    """
    prefix_top = f'row-'  # row-N-top (нумерация сквозная по документу)
    # Для multi-table: tab_id определяет диапазон строк
    # Пока поддерживаем одну таблицу — все row-N-top/bot

    # ── Собираем строки ──
    rows_data = {}
    for n in range(1, total_rows + 1):
        key_top = f'row-{n}-top'
        key_bot = f'row-{n}-bot'
        if key_top not in labels or key_bot not in labels:
            continue
        top  = labels[key_top]['y']
        bot  = labels[key_bot]['y']
        page = labels[key_top]['page']
        rows_data[n] = {
            'top':  top,
            'bot':  bot,
            'page': page,
            'h':    top - bot,
        }

    tab_bot_key = f'tab-{tab_id}-bot'
    tab_bot_y   = labels.get(tab_bot_key, {}).get('y', 0)
    tab_bot_pg  = labels.get(tab_bot_key, {}).get('page', 0)

    # ── Группируем по страницам ──
    pages_map = {}
    for rn, rd in rows_data.items():
        pages_map.setdefault(rd['page'], []).append(rn)
    for p in pages_map:
        pages_map[p].sort()

    # ── Высота шапки ──
    # Первая строка данных на второй и последующих страницах начинается
    # ниже шапки endhead. Верх первой строки каждой страницы = page_top_after_head.
    # Шапка endhead = page_top (802pt примерно) - first_row_top
    # Используем строку на стр.2+ (не стр.1 где endfirsthead другая)
    head_pt = 0
    sorted_pages = sorted(pages_map.keys())
    if len(sorted_pages) >= 2:
        p2 = sorted_pages[1]
        first_row_on_p2 = pages_map[p2][0]
        if first_row_on_p2 in rows_data:
            # page_top в posy = textheight + bottom (приближённо)
            page_content_top = rows_data[first_row_on_p2]['top']
            # endhead height = расстояние от page_content_top до верха текста
            # Верх текста ≈ bottom + textheight = 85.36 + 717 = 802.36
            page_top_approx = bottom + 717.01  # textheight
            head_pt = round(page_top_approx - page_content_top)

    # ── Пробелы и добавки ──
    page_gaps = {}
    adds = {}

    for p in sorted_pages:
        row_list = pages_map[p]
        last     = row_list[-1]

        if last == total_rows and rows_data[last]['page'] == tab_bot_pg:
            gap = tab_bot_y - bottom
        elif last in rows_data:
            gap = rows_data[last]['bot'] - bottom
        else:
            gap = 0.0

        page_gaps[p] = {'gap': gap, 'last': last}

        stretchable = [r for r in row_list if r != last and r != total_rows]
        if stretchable:
            gap_per = gap / len(stretchable)
            for r in stretchable:
                h     = rows_data[r]['h']
                xpt   = round(h + gap_per - overhead)
                avail = rows_data[r]['top'] - bottom
                if xpt + overhead > avail:
                    xpt = round(avail - overhead - 1)
                adds[r] = xpt

    # ── Формируем JSON структуру ──

    # pages секция
    pages_json = {}
    for p in sorted_pages:
        row_list    = pages_map[p]
        last        = page_gaps[p]['last']
        gap         = page_gaps[p]['gap']
        stretchable = [r for r in row_list if r != last and r != total_rows]
        pages_json[str(p)] = {
            'rows':        row_list,
            'last_row':    last,
            'gap_pt':      round(gap, 1),
            'stretchable': stretchable,
        }

    # rows секция — каждая строка
    rows_json = {}
    for n in range(1, total_rows + 1):
        if n not in rows_data:
            continue
        rd  = rows_data[n]
        gap = rd['bot'] - bottom
        rows_json[str(n)] = {
            'page':   rd['page'],
            'h_pt':   round(rd['h'], 1),
            'add_pt': adds.get(n, 0),
            'gap_pt': round(gap, 1),
        }

    return {
        'head':  head_pt,
        'pages': pages_json,
        'rows':  rows_json,
    }


# ─────────────────────────────────────────────
# Команды CLI
# ─────────────────────────────────────────────

def cmd_prepare(input_tex: str, output_tex: str):
    with open(input_tex, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    new_lines = []
    i = 0
    row_num  = 0
    in_data  = False

    while i < len(lines):
        line    = lines[i]
        stripped = line.rstrip('\n')

        if r'\endhead%' in stripped:
            in_data = True
            new_lines.append(line)
            i += 1
            continue

        if in_data and r'\multicolumn{10}{|c|}{}%' in stripped:
            in_data = False
            new_lines.append('\t\t\t\\multicolumn{10}{|c|}{\\zsavepos{tab-1-bot}}%\n')
            new_lines.append('\t\t\t\\\\%\n')
            new_lines.append('\t\t\t\\hline%\n')
            i += 1
            while i < len(lines) and lines[i].strip().rstrip('%') in ['\\\\', '\\hline', '']:
                i += 1
            continue

        if in_data and re.match(r'\s+&\d+&', stripped):
            row_num += 1
            m      = re.match(r'^(\s+)&', stripped)
            indent = m.group(1)
            rest   = stripped[len(indent) + 1:]
            rest   = re.sub(r'\\\\(\[\d+pt\])?%\s*$', r'\\\\%', rest)

            zpos_top = '\\zsavepos{row-' + str(row_num) + '-top}'
            new_lines.append(indent + '\\multicolumn{1}{|c|}{' + zpos_top + '}&' + rest + '\n')
            i += 1

            while i < len(lines) and (
                r'\cline{2' in lines[i] or
                lines[i].strip().rstrip('%') in ['-', '9}']
            ):
                new_lines.append(lines[i])
                i += 1

            zpos_bot = '\\zsavepos{row-' + str(row_num) + '-bot}'
            new_lines.append(indent + '\\noalign{' + zpos_bot + '}%\n')
            continue

        new_lines.append(line)
        i += 1

    with open(output_tex, 'w', encoding='utf-8') as f:
        f.writelines(new_lines)

    print(f"[prepare] Метки вставлены для {row_num} строк → {output_tex}")
    print(f"[prepare] Следующий шаг: lualatex {output_tex}")


def cmd_apply(input_tex: str, aux_path: str, output_tex: str, overhead: float):
    labels = parse_aux(aux_path)
    bottom = get_bottom(aux_path)

    # Определяем total_rows и таблицы
    n = 1
    while f'row-{n}-top' in labels:
        n += 1
    total_rows = n - 1

    # Строим состояние и получаем добавки
    state = build_table_state(labels, 1, total_rows, bottom, overhead)
    adds  = {int(k): v['add_pt'] for k, v in state['rows'].items() if v['add_pt'] > 0}

    print(f"\n[apply] Добавки: {adds}")

    # Применяем в .tex
    with open(input_tex, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    new_lines = []
    i = 0
    row_num  = 0
    in_data  = False

    while i < len(lines):
        line    = lines[i]
        stripped = line.rstrip('\n')

        if r'\endhead%' in stripped:
            in_data = True
            new_lines.append(line)
            i += 1
            continue

        if in_data and r'\multicolumn{10}{|c|}{}%' in stripped:
            in_data = False
            new_lines.append('\t\t\t\\multicolumn{10}{|c|}{\\zsavepos{tab-1-bot}}%\n')
            new_lines.append('\t\t\t\\\\%\n')
            new_lines.append('\t\t\t\\hline%\n')
            i += 1
            while i < len(lines) and lines[i].strip().rstrip('%') in ['\\\\', '\\hline', '']:
                i += 1
            continue

        if in_data and re.match(r'\s+&\d+&', stripped):
            row_num += 1
            m      = re.match(r'^(\s+)&', stripped)
            indent = m.group(1)
            rest   = stripped[len(indent) + 1:]
            rest   = re.sub(r'\\\\(\[\d+pt\])?%\s*$', r'\\\\%', rest)

            if row_num in adds:
                rest = re.sub(r'\\\\%\s*$', f'\\\\\\\\[{adds[row_num]}pt]%', rest)

            zpos_top = '\\zsavepos{row-' + str(row_num) + '-top}'
            new_lines.append(indent + '\\multicolumn{1}{|c|}{' + zpos_top + '}&' + rest + '\n')
            i += 1

            while i < len(lines) and (
                r'\cline{2' in lines[i] or
                lines[i].strip().rstrip('%') in ['-', '9}']
            ):
                new_lines.append(lines[i])
                i += 1

            zpos_bot = '\\zsavepos{row-' + str(row_num) + '-bot}'
            new_lines.append(indent + '\\noalign{' + zpos_bot + '}%\n')
            continue

        new_lines.append(line)
        i += 1

    with open(output_tex, 'w', encoding='utf-8') as f:
        f.writelines(new_lines)

    print(f"[apply] Записан: {output_tex}")


def cmd_inspect(tex_path: str, aux_path: str, json_out: str | None,
                overhead: float):
    labels = parse_aux(aux_path)
    bottom = get_bottom(aux_path)

    # Получаем метаданные таблиц из .tex
    tables_meta = parse_tables_from_tex(tex_path)
    if not tables_meta:
        # Fallback — одна таблица, caption неизвестен
        n = 1
        while f'row-{n}-top' in labels:
            n += 1
        tables_meta = [{'caption': 'table-1', 'tab_id': 1, 'total_rows': n - 1}]

    result = {'tables': {}}

    for meta in tables_meta:
        caption    = meta['caption']
        tab_id     = meta['tab_id']
        total_rows = meta['total_rows']

        if total_rows == 0:
            continue

        state = build_table_state(labels, tab_id, total_rows, bottom, overhead)
        result['tables'][caption] = state

    # Компактный формат для вывода (как в примере пользователя)
    compact = {'tables': {}}
    for caption, state in result['tables'].items():
        entry = {'head': state['head']}
        for rn, rd in state['rows'].items():
            entry[rn] = rd['add_pt']
        compact['tables'][caption] = entry

    # Полный формат
    output = {
        'tables':  result['tables'],
        'compact': compact,
    }

    json_str = json.dumps(output, ensure_ascii=False, indent=2)

    if json_out:
        with open(json_out, 'w', encoding='utf-8') as f:
            f.write(json_str)
        print(f"[inspect] JSON записан: {json_out}")

    print(json_str)
    return output


# ─────────────────────────────────────────────
# main
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Автозаполнение пробелов в LaTeX таблицах ltablex/longtable'
    )
    sub = parser.add_subparsers(dest='cmd')

    p1 = sub.add_parser('prepare', help='Вставить zsavepos-метки в .tex')
    p1.add_argument('input',  help='Исходный .tex')
    p1.add_argument('output', help='Выходной .tex с метками')

    p2 = sub.add_parser('apply', help='Применить добавки \\\\[Xpt] из .aux')
    p2.add_argument('input',  help='.tex с метками (от prepare)')
    p2.add_argument('aux',    help='.aux после компиляции')
    p2.add_argument('output', help='Результирующий .tex')
    p2.add_argument('--overhead', type=float, default=OVERHEAD)

    p3 = sub.add_parser('inspect', help='Показать состояние таблиц в JSON')
    p3.add_argument('input', help='.tex с метками')
    p3.add_argument('aux',   help='.aux после компиляции')
    p3.add_argument('--json',     dest='json_out', default=None,
                    help='Сохранить JSON в файл')
    p3.add_argument('--overhead', type=float, default=OVERHEAD)

    args = parser.parse_args()

    if args.cmd == 'prepare':
        cmd_prepare(args.input, args.output)
    elif args.cmd == 'apply':
        cmd_apply(args.input, args.aux, args.output, args.overhead)
    elif args.cmd == 'inspect':
        cmd_inspect(args.input, args.aux, args.json_out, args.overhead)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
