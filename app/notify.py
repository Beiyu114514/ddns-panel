import os

import httpx


async def send_tg(token: str, chat_id: str, text: str) -> tuple[bool, str]:
    """通过 Telegram Bot API 发送消息，返回 (是否成功, 说明)。"""
    if not token or not chat_id:
        return False, "未配置 bot token 或 chat id"
    proxy = os.environ.get("TG_PROXY") or None
    try:
        async with httpx.AsyncClient(timeout=15, proxy=proxy) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            )
            data = r.json()
            if data.get("ok"):
                return True, "已发送"
            return False, f"Telegram 返回错误: {data.get('description', r.status_code)}"
    except Exception as e:
        return False, f"请求失败: {e}"
