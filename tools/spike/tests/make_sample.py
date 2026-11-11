import sys
from docx import Document
d = Document()
d.add_heading("Sample Chapter: Aneurysm Surveillance", 1)
d.add_paragraph("This synthetic test paragraph states that an abdominal aortic aneurysm of 5.5 cm in men is a common repair threshold, with surveillance every 12 months below 4.0 cm.[1]")
d.add_heading("Follow-up", 2)
d.add_paragraph("After endovascular aneurysm repair, imaging at 30 days checks for endoleak in roughly 20% of patients.")
t = d.add_table(rows=2, cols=2)
t.cell(0,0).text="Diameter"; t.cell(0,1).text="Interval"
t.cell(1,0).text="3.0-3.9 cm"; t.cell(1,1).text="Every 3 years"
d.add_heading("References", 2)
d.add_paragraph("1. Sample AB, Author CD. Synthetic reference. J Test. 2020;1:1-2.")
d.save(sys.argv[1])
