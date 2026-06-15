to extract data from screen shots for order status files and put into .csv form for excel or similar spreadsheets, in this case the data come from chase order status image in pdf form.

to run use, python chase_orders.py file1.png file2.png (etc) -o outfile.csv
this was developed entirely with cursor

also with cursor-

The USB holdings .pdf to .csv program works as of 6-12-2026,
run with-
python USB_holdings_pdf_to_csv.py "path\to\holdings.pdf" output.csv

It needs three pip packages (pymupdf, winocr, pillow) and uses Windows' built-in OCR engine.

the USB holdings .pdf to .csv program works as of 6-12-2026, cursor (with Fable at that time) mentions the program is set for the current USB layout for the pdf and if USB redesigns the bank pdf layout then the program USB_holdings_pdf_to_csv.py will probably need to change as well.
