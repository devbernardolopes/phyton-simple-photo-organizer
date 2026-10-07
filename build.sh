#!/usr/bin/env bash
# Gera dist/OrganizadorDeFotos (binário Linux para TESTE local).
# ATENÇÃO: este binário NÃO roda no Windows do seu sogro.
# O .exe de verdade é gerado pelo GitHub Actions (workflow build-windows.yml).
#
# Uso:
#   chmod +x build.sh
#   ./build.sh
#
# Requisito no Omarchy/Arch:
#   sudo pacman -S --needed python tk
#   (o script avisa se o Tk estiver faltando)

set -euo pipefail

cd "$(dirname "$0")"

echo "==> Verificando Python..."
if ! command -v python3 >/dev/null 2>&1; then
  echo "ERRO: python3 não encontrado. No Arch/Omarchy: sudo pacman -S python" >&2
  exit 1
fi
python3 --version

echo "==> Verificando Tkinter (necessário para a interface gráfica)..."
if ! python3 -c "import tkinter" 2>/dev/null; then
  echo "" >&2
  echo "ERRO: Tkinter não está funcional (falta libtk8.6.so)." >&2
  echo "No Omarchy/Arch, instale com:" >&2
  echo "  sudo pacman -S --needed tk" >&2
  echo "" >&2
  echo "Depois rode ./build.sh novamente." >&2
  exit 1
fi
echo "Tkinter OK."

VENV_DIR=".venv-linux"

echo "==> Criando venv em ${VENV_DIR}..."
if [ ! -d "${VENV_DIR}" ]; then
  python3 -m venv "${VENV_DIR}"
fi

# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"

echo "==> Atualizando pip e instalando dependências..."
python -m pip install --upgrade pip
pip install -r requirements.txt pyinstaller

echo "==> Gerando executável Linux com PyInstaller..."
pyinstaller --onefile --windowed --name OrganizadorDeFotos organizador_fotos.py --clean

echo ""
echo "Pronto: dist/OrganizadorDeFotos"
echo "Teste com: ./dist/OrganizadorDeFotos"
echo "(Lembrete: para o Windows, use o .exe gerado pelo GitHub Actions.)"
