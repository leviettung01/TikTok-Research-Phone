"""Cách 2 (Tùy chọn): Đọc thông tin sản phẩm từ trình duyệt Chrome (nếu khả dụng).

Lưu ý:
- Hiện tại TikTok Shop Web Desktop thường trả về lỗi chặn vùng quốc gia:
  {"message": "The product is not currently available in your country or region."}
  (URL redirect chứa first_entrance_position=region_not_match) khi IP/Cookie/Tài khoản
  trên trình duyệt lệch vùng hoặc TikTok khóa xem PDP trên trình duyệt máy tính.
- Module này kiểm tra nhanh phản hồi trang: nếu gặp lỗi chặn vùng (region_not_match),
  trang trống hoặc yêu cầu xác minh trên trình duyệt, sẽ DỪNG NGAY LẬP TỨC (không treo chờ)
  để hệ thống tự động chuyển về dùng Cách 1 (SPAI Auto API) mượt mà.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

import config

LogFn = Callable[[str], None]


def _default_log(msg: str) -> None:
    try:
        print(f"[BrowserCrawler] {msg}")
    except UnicodeEncodeError:
        pass


SCRAPE_PRODUCT_JS = r"""
() => {
    const bodyText = (document.body && document.body.innerText || '').trim();
    if (
        bodyText.includes('not currently available in your country or region') ||
        location.href.includes('region_not_match')
    ) {
        return { region_blocked: true, raw_message: bodyText.slice(0, 300) };
    }

    const hasVerifyGate = !!(
        document.querySelector('#captcha-verify-image') ||
        document.querySelector('[class*="captcha_verify"]') ||
        document.querySelector('[class*="secsdk-captcha"]')
    );
    if (hasVerifyGate) {
        return { verify_gated: true };
    }

    const result = {
        title: '',
        price: '',
        original_price: '',
        sold_count: '',
        seller: '',
        description: '',
        images: [],
        desc_images: [],
        variations: []
    };

    const isProductImg = (url) => {
        if (!url || !url.startsWith('http')) return false;
        const lower = url.toLowerCase();
        if (lower.includes('avatar') || lower.includes('icon') || lower.includes('logo') ||
            lower.includes('badge') || lower.includes('flag') || lower.includes('captcha') ||
            lower.includes('verify') || lower.includes('100x100') || lower.includes('64x64') ||
            lower.includes('72x72') || lower.includes('50x50') || lower.includes('.svg') ||
            lower.includes('.gif')) return false;
        return lower.includes('ibyteimg.com') || lower.includes('tiktokcdn.com') || lower.includes('ttcdn');
    };

    const seenImg = new Set();
    const addImg = (u, targetArr) => {
        if (!isProductImg(u)) return;
        const base = u.split('?')[0].replace(/~tplv-[^/]+/, '');
        if (!seenImg.has(base)) {
            seenImg.add(base);
            targetArr.push(u);
        }
    };

    const scripts = document.querySelectorAll('script');
    for (const s of scripts) {
        const text = (s.textContent || '').trim();
        if (!text || text.length < 100) continue;
        let jsonData = null;
        if (['__MODERN_ROUTER_DATA__', '__UNIVERSAL_DATA_FOR_WEB__', '__UNIVERSAL_DATA_FOR_REHYDRATION__', 'RENDER_DATA', '__NEXT_DATA__'].includes(s.id)) {
            try { jsonData = JSON.parse(decodeURIComponent(text)); } catch (e) {
                try { jsonData = JSON.parse(text); } catch (e2) {}
            }
        }
        if (jsonData) {
            const walk = (obj, depth = 0) => {
                if (!obj || typeof obj !== 'object' || depth > 14) return;
                if (Array.isArray(obj)) {
                    for (const item of obj) walk(item, depth + 1);
                    return;
                }
                const pBase = obj.product_base || obj.product_info || ((obj.product_id && obj.title && Array.isArray(obj.images)) ? obj : null);
                if (!result.title && pBase && typeof pBase.title === 'string' && pBase.title.length > 5) {
                    result.title = pBase.title;
                }
                const prObj = (pBase && pBase.price && typeof pBase.price === 'object') ? pBase.price : null;
                if (prObj) {
                    if (!result.price) result.price = prObj.real_price || prObj.sale_price || prObj.format_price || prObj.min_sku_price || '';
                    if (!result.original_price) result.original_price = prObj.original_price || prObj.min_sku_original_price || '';
                }
                if (!result.sold_count && pBase && pBase.sold_count !== undefined && typeof pBase.sold_count !== 'object') {
                    result.sold_count = String(pBase.sold_count);
                }
                if (!result.seller && pBase && pBase.seller && typeof pBase.seller.name === 'string') {
                    result.seller = pBase.seller.name;
                }
                if (!result.seller && obj.shop_info && typeof obj.shop_info.shop_name === 'string') {
                    result.seller = obj.shop_info.shop_name;
                }
                if (pBase && Array.isArray(pBase.images) && result.images.length === 0) {
                    for (const item of pBase.images) {
                        if (!item) continue;
                        const urls = item.url_list || item.urls || (item.url ? [item.url] : []);
                        if (Array.isArray(urls) && urls.length > 0) addImg(urls[0], result.images);
                        else if (typeof item === 'string') addImg(item, result.images);
                    }
                }
                if (pBase && Array.isArray(pBase.desc_blocks)) {
                    const textBlocks = [];
                    for (const blk of pBase.desc_blocks) {
                        if (!blk) continue;
                        const txt = blk.text || blk.content || blk.plain_text;
                        if (typeof txt === 'string' && txt.trim()) textBlocks.push(txt.trim());
                        if (blk.image && Array.isArray(blk.image.url_list) && blk.image.url_list.length > 0) {
                            addImg(blk.image.url_list[0], result.desc_images);
                        }
                    }
                    if (textBlocks.length > 0 && !result.description) result.description = textBlocks.join('\n');
                }
                for (const k of Object.keys(obj)) walk(obj[k], depth + 1);
            };
            walk(jsonData, 0);
        }
    }

    if (!result.title) {
        const el = document.querySelector('h1, [data-testid*="product-title"], [class*="product-title"], [class*="pdp-title"]');
        if (el && el.innerText && el.innerText.trim().length > 5) result.title = el.innerText.trim();
    }
    if (result.images.length === 0) {
        document.querySelectorAll('img').forEach(img => {
            const src = img.src || img.getAttribute('data-src') || '';
            const w = img.naturalWidth || img.width || 0;
            if (w > 0 && w < 180) return;
            addImg(src, result.images);
        });
    }
    return result;
}
"""


def _find_chrome_exe() -> str | None:
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def _resolve_tiktokshop_profile_dir() -> Path:
    candidates = [
        config.TIKTOK_SHOP_PROFILE_DIR,
        Path.home() / ".tiktok_img_tool" / "profiles" / "tiktok_chrome_profile",
    ]
    for p in candidates:
        try:
            if p.exists() and (p / "Default").exists():
                return p
        except Exception:
            continue
    config.TIKTOK_SHOP_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    return config.TIKTOK_SHOP_PROFILE_DIR


def is_chrome_cdp_alive(port: int | None = None) -> bool:
    port = port or config.CHROME_DEBUG_PORT
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1.5) as resp:
            return resp.status == 200
    except Exception:
        return False


def ensure_chrome_cdp_running(log_fn: LogFn = _default_log) -> bool:
    port = config.CHROME_DEBUG_PORT
    if is_chrome_cdp_alive(port):
        return True
    chrome_exe = _find_chrome_exe()
    if not chrome_exe:
        log_fn("❌ Không tìm thấy Google Chrome trên máy tính.")
        return False
    profile_dir = _resolve_tiktokshop_profile_dir()
    cmd = [
        chrome_exe,
        f"--user-data-dir={profile_dir}",
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        "https://www.tiktok.com",
    ]
    creation_flags = 0x00000008 | 0x00000200 if os.name == "nt" else 0
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=creation_flags)
    for _ in range(16):
        time.sleep(0.5)
        if is_chrome_cdp_alive(port):
            return True
    return False


def _parse_money_to_float(raw: Any) -> float:
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw or "").strip()
    if not s:
        return 0.0
    if "-" in s:
        s = s.split("-")[0].strip()
    digits = re.sub(r"[^\d]", "", s)
    return float(digits) if digits else 0.0


def _parse_sold_count(raw: Any) -> tuple[int, str]:
    if isinstance(raw, int):
        return raw, str(raw)
    s = str(raw or "").strip()
    if not s:
        return 0, ""
    low = s.lower().replace("đã bán", "").replace("sold", "").replace("+", "").strip()
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*([km]?)", low)
    if not m:
        return 0, s
    num_str, suffix = m.group(1), m.group(2)
    if suffix == "k":
        val = int(float(num_str.replace(",", ".")) * 1000)
    elif suffix == "m":
        val = int(float(num_str.replace(",", ".")) * 1_000_000)
    else:
        val = int(re.sub(r"[^\d]", "", num_str) or "0")
    return val, s


_BROWSER_CRAWL_LOCK = threading.Lock()


def fetch_product_details_via_browser(
    product_id_or_url: str,
    download_images: bool = True,
    log_fn: LogFn = _default_log,
) -> dict[str, Any]:
    """Thử đọc thông tin sản phẩm qua trình duyệt Chrome.
    Nếu gặp chặn vùng quốc gia ("The product is not currently available in your country or region.")
    hoặc lỗi bất kỳ -> thoát ngay lập tức để chuyển sang Cách 1 (SPAI Auto).
    """
    raw = (product_id_or_url or "").strip()
    m = re.search(r"(?:pdp/|/product/|/products/|^)(\d{15,22})", raw)
    pid = m.group(1) if m else (raw if raw.isdigit() else "")
    if not pid:
        return {"success": False, "error": f"Không nhận diện được ID sản phẩm: {raw}"}

    pdp_url = f"https://shop.tiktok.com/vn/pdp/{pid}"
    urls_to_try = [
        f"https://www.tiktok.com/view/product/{pid}",
        pdp_url,
    ]

    scraped: dict[str, Any] = {}
    error_reason = ""

    with _BROWSER_CRAWL_LOCK:
        cdp_ready = ensure_chrome_cdp_running(log_fn=log_fn)
        if not cdp_ready:
            return {"success": False, "product_id": pid, "error": "Chrome CDP chưa sẵn sàng."}
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{config.CHROME_DEBUG_PORT}")
                ctx = browser.contexts[0] if browser.contexts else browser.new_context()
                page = ctx.new_page()
                try:
                    for target_url in urls_to_try:
                        log_fn(f"🌐 [Cách 2 - Browser] Đang kiểm tra: {target_url}")
                        try:
                            page.goto(target_url, wait_until="domcontentloaded", timeout=15000)
                        except Exception:
                            pass
                        time.sleep(1.2)

                        data = page.evaluate(SCRAPE_PRODUCT_JS)
                        if isinstance(data, dict):
                            if data.get("region_blocked"):
                                error_reason = "TikTok Web chặn vùng quốc gia (The product is not currently available in your country or region / region_not_match)."
                                log_fn(f"⚠️ [Cách 2] {error_reason} -> Dừng ngay để chuyển sang Cách 1!")
                                break
                            if data.get("verify_gated"):
                                error_reason = "Trang trình duyệt yêu cầu xác minh thủ công."
                                log_fn(f"⚠️ [Cách 2] {error_reason} -> Dừng ngay để chuyển sang Cách 1!")
                                break

                            title_cand = str(data.get("title") or "").strip()
                            imgs_cand = data.get("images") or []
                            if title_cand.lower() in ("tiktok", "tiktok shop", "security check", "verify"):
                                title_cand = ""
                                data["title"] = ""
                            if title_cand or len(imgs_cand) > 0:
                                scraped = data
                                break
                finally:
                    try:
                        page.close()
                    except Exception:
                        pass
        except Exception as exc:
            error_reason = f"Lỗi trình duyệt: {exc}"

    title = str(scraped.get("title") or "").strip()
    images: list[str] = [str(u).strip() for u in (scraped.get("images") or []) if str(u).strip()]

    if not title and not images:
        return {
            "success": False,
            "product_id": pid,
            "error": error_reason or "Cách 2 (Browser) không lấy được thông tin sản phẩm.",
        }

    shop_name = str(scraped.get("seller") or "").strip()
    price = int(_parse_money_to_float(scraped.get("price")))
    orig_price = int(_parse_money_to_float(scraped.get("original_price")))
    if 0 <= price <= 100 and orig_price > 1000:
        price = orig_price
    sold_count, sold_label = _parse_sold_count(scraped.get("sold_count"))
    desc = str(scraped.get("description") or "").strip()
    desc_images: list[str] = [str(u).strip() for u in (scraped.get("desc_images") or []) if str(u).strip()]
    variations: list[Any] = scraped.get("variations") if isinstance(scraped.get("variations"), list) else []
    if not images and desc_images:
        images = list(desc_images[:10])

    primary_image = images[0] if images else ""
    local_thumb_path = ""
    local_images: list[str] = []

    if download_images and pid:
        prod_dir = config.PRODUCT_IMAGES_DIR / pid
        images_dir = prod_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        thumb_file = prod_dir / "thumb.webp"
        headers_dl = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
            "Referer": "https://www.tiktok.com/",
        }
        for idx, img_url in enumerate(images[:15]):
            target_file = images_dir / f"img_{idx}.webp"
            if not target_file.exists() or target_file.stat().st_size < 200:
                try:
                    req_img = urllib.request.Request(img_url, headers=headers_dl)
                    with urllib.request.urlopen(req_img, timeout=15) as r_img:
                        img_bytes = r_img.read()
                        if len(img_bytes) > 200:
                            target_file.write_bytes(img_bytes)
                            if idx == 0 and (not thumb_file.exists() or thumb_file.stat().st_size < 200):
                                thumb_file.write_bytes(img_bytes)
                except Exception:
                    pass
            if target_file.is_file() and target_file.stat().st_size > 200:
                local_images.append(str(target_file))
        if thumb_file.exists() and thumb_file.stat().st_size > 200:
            local_thumb_path = f"product_images/{pid}/thumb.webp"

        info_file = prod_dir / "info.json"
        try:
            info_file.write_text(json.dumps({
                "product_id": pid,
                "pdp_url": pdp_url,
                "title": title,
                "shop_name": shop_name,
                "price": price,
                "original_price": orig_price,
                "sold_count": sold_count,
                "sold_count_label": sold_label,
                "description": desc,
                "images": images,
                "description_images": desc_images,
                "variations": variations,
                "local_images": local_images,
                "source": "browser_tiktokshop",
                "fetched_at": time.time(),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    return {
        "success": True,
        "product_id": pid,
        "pdp_url": pdp_url,
        "title": title,
        "shop_name": shop_name,
        "price": price,
        "original_price": orig_price,
        "sold_count": sold_count,
        "sold_count_label": sold_label,
        "description": desc,
        "images": images,
        "desc_images": desc_images,
        "variations": variations,
        "image_url": primary_image,
        "local_thumb": local_thumb_path,
        "total_images": len(images),
        "source": "browser_tiktokshop",
    }
