"""
Єдина точка входу для Railway.
Запускає Flask адмін-панель у фоновому потоці і бота в основному.
"""
import threading
import os
import admin_panel
import bot

def run_admin():
    port = int(os.getenv("PORT", 8080))
    print(f"Admin panel → http://0.0.0.0:{port}")
    admin_panel.app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

# Flask у фоновому потоці (daemon — автоматично зупиняється разом з ботом)
t = threading.Thread(target=run_admin, daemon=True)
t.start()

# Бот у головному потоці (asyncio event loop)
bot.main()
