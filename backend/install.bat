@echo off
echo Installazione dipendenze backend...
pip install -r requirements.txt
echo.
echo Setup completo. Avvia il backend con:
echo   uvicorn main:app --reload
