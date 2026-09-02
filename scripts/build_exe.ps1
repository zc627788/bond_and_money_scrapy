python -m pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm --clean --windowed --onefile `
  --name BondMoneyScrapy `
  --hidden-import openpyxl `
  --hidden-import pandas `
  --hidden-import curl_cffi `
  --add-data "config/settings.json;config" `
  --add-data "templates/issuers_template.csv;templates" `
  gui_app.py
Write-Host "exe: dist\BondMoneyScrapy.exe"
