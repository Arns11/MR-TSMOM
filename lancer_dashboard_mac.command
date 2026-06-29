#!/bin/bash
cd "$(dirname "$0")"

echo "Lancement dashboard Combo XNDX MR + TSMOM..."
echo ""

python3 -c "import streamlit" 2>/dev/null
if [ $? -ne 0 ]; then
    echo "Installation streamlit..."
    pip3 install streamlit pandas numpy matplotlib
fi

streamlit run dashboard/app.py
