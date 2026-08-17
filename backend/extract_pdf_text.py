from pathlib import Path
import PyPDF2
pdf_path = Path(r'C:\Users\Admin\Downloads\ROOTPLAN_Project_Documentation.pdf')
print('exists', pdf_path.exists())
reader = PyPDF2.PdfReader(str(pdf_path))
print('pages', len(reader.pages))
for i, page in enumerate(reader.pages, start=1):
    print('--- PAGE', i, '---')
    text = page.extract_text() or ''
    print(text.replace('\r', '\n'))
