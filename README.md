# auto_height

# 1. Вставить метки
python latex_fill_gaps.py prepare  doc.tex  doc_marked.tex

# 2. Применить добавки
python latex_fill_gaps.py apply    doc_marked.tex  doc_marked.aux  doc_result.tex

# 3. Посмотреть состояние в JSON
python latex_fill_gaps.py inspect  doc_marked.tex  doc_marked.aux
python latex_fill_gaps.py inspect  doc_marked.tex  doc_marked.aux  --json state.json