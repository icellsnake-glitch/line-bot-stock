# ===== Rich Menu：一鍵建立 / 上傳圖片 / 清除 =====
from io import BytesIO
from linebot.models import RichMenu, RichMenuArea, RichMenuSize, RichMenuBounds, MessageAction

def _check_key():
    return (not CRON_SECRET) or (request.args.get("key") == CRON_SECRET)

def _find_default_richmenu():
    try:
        return line_bot_api.get_default_rich_menu()
    except Exception:
        return None

def _list_richmenus():
    try:
        return line_bot_api.get_rich_menu_list()
    except Exception:
        return []

@app.get("/setup-richmenu")
def setup_richmenu():
    if not _check_key():
        return "Unauthorized", 401

    # 若已存在預設 Rich Menu，先跳過或清掉
    try:
        current = _find_default_richmenu()
        if current:
            return "Default rich menu already exists.", 200
    except Exception:
        pass

    # 2x2 佈局：1200 x 810
    size = RichMenuSize(width=1200, height=810)
    areas = [
        # 左上：盤中
        RichMenuArea(
            bounds=RichMenuBounds(x=0,   y=0,   width=600, height=405),
            action=MessageAction(label="盤中", text="盤中")
        ),
        # 右上：隔日
        RichMenuArea(
            bounds=RichMenuBounds(x=600, y=0,   width=600, height=405),
            action=MessageAction(label="隔日", text="隔日")
        ),
        # 左下：測試
        RichMenuArea(
            bounds=RichMenuBounds(x=0,   y=405, width=600, height=405),
            action=MessageAction(label="測試", text="測試")
        ),
        # 右下：幫助
        RichMenuArea(
            bounds=RichMenuBounds(x=600, y=405, width=600, height=405),
            action=MessageAction(label="幫助", text="幫助")
        ),
    ]

    richmenu = RichMenu(
        size=size,
        selected=True,                 # 建立後就能設成預設
        name="Stock Scanner Menu",
        chat_bar_text="選擇功能",
        areas=areas
    )

    try:
        richmenu_id = line_bot_api.create_rich_menu(rich_menu=richmenu)
        # 設為預設
        line_bot_api.set_default_rich_menu(richmenu_id)
        return f"Rich menu created and set default. id={richmenu_id}", 200
    except Exception as e:
        app.logger.exception(e)
        return str(e), 500

@app.get("/upload-richmenu")
def upload_richmenu():
    if not _check_key():
        return "Unauthorized", 401

    img_url = request.args.get("img", "").strip()
    if not img_url:
        return "請用 ?img=<圖片URL> 指定 1200x810 PNG/JPG 圖片", 400

    # 找到目前預設 rich menu
    try:
        default_rm = _find_default_richmenu()
        if not default_rm:
            # 若沒有預設，就找列表第一個
            rms = _list_richmenus()
            if not rms:
                return "沒有可用的 rich menu，請先呼叫 /setup-richmenu", 400
            richmenu_id = rms[0].rich_menu_id
        else:
            richmenu_id = default_rm.rich_menu_id
    except Exception as e:
        app.logger.exception(e)
        return "取得 rich menu 失敗", 500

    # 下載圖片並上傳
    try:
        r = requests.get(img_url, timeout=15)
        r.raise_for_status()
        content = r.content

        # 猜格式
        ctype = r.headers.get("Content-Type", "").lower()
        if "png" in ctype:
            content_type = "image/png"
        elif "jpeg" in ctype or "jpg" in ctype:
            content_type = "image/jpeg"
        else:
            # 簡單判斷 magic bytes
            if content[:8] == b"\x89PNG\r\n\x1a\n":
                content_type = "image/png"
            else:
                content_type = "image/jpeg"

        line_bot_api.set_rich_menu_image(richmenu_id, content_type, BytesIO(content))
        return f"Rich menu image uploaded to {richmenu_id}", 200
    except Exception as e:
        app.logger.exception(e)
        return str(e), 500

@app.get("/clear-richmenu")
def clear_richmenu():
    if not _check_key():
        return "Unauthorized", 401

    try:
        # 先解除預設
        try:
            line_bot_api.cancel_default_rich_menu()
        except Exception:
            pass

        # 刪除所有 rich menu
        rms = _list_richmenus()
        cnt = 0
        for rm in rms:
            try:
                line_bot_api.delete_rich_menu(rm.rich_menu_id)
                cnt += 1
            except Exception:
                pass
        return f"Cleared {cnt} rich menu(s).", 200
    except Exception as e:
        app.logger.exception(e)
        return str(e), 500