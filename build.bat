@echo off
rem Gera dist\OrganizadorDeFotos.exe (nao exige Python na maquina de destino)
python -m venv .venv
call .venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt pyinstaller
pyinstaller --onefile --windowed --name OrganizadorDeFotos organizador_fotos.py
echo.
echo Pronto: dist\OrganizadorDeFotos.exe
pause