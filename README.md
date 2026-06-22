# 🎬 Kino Bot

## Ishga tushirish

### 1. Paketlarni o'rnatish
```bash
pip install -r requirements.txt
```

### 2. .env faylini sozlash
```
BOT_TOKEN=sizning_bot_tokeningiz
ADMIN_ID=sizning_telegram_id_ingiz
```

### 3. Ishga tushirish
```bash
python main.py
```

---

## Serverga (Linux VPS) joylashtirish

```bash
# 1. Fayllarni serverga nusxa oling
scp -r kinobot/ root@server_ip:/root/kinobot

# 2. Serverda .env fayl yarating
nano /root/kinobot/.env

# 3. Paketlarni o'rnating
cd /root/kinobot
pip3 install -r requirements.txt

# 4. Systemd service o'rnating
cp kino_bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable kino_bot
systemctl start kino_bot

# 5. Holatini tekshirish
systemctl status kino_bot
```
