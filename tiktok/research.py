"""AI Research Sản phẩm — Tìm kiếm, bắt link tự động và quản lý kho sản phẩm TikTok Shop Affiliate.

Tối ưu hóa cho bản Độc lập (TikTok Research & Phone Studio):
- Mặc định dùng Cách 1 (SPAI Auto API) tốc độ cao, tải trọn bộ Ảnh nét, Giá, Đã bán, Tên Shop,
  Phân loại và Mô tả chi tiết mà không cần bật trình duyệt Chrome.
- Nếu người dùng thử Cách 2 (Browser) mà gặp lỗi TikTok chặn vùng quốc gia
  ({"message": "The product is not currently available in your country or region."} / region_not_match)
  hoặc bất kỳ lỗi trình duyệt nào -> Hệ thống TỰ ĐỘNG CHUYỂN NGAY VỀ DÙNG CÁCH 1 (SPAI Auto).
"""
from __future__ import annotations

import io
import json
import re
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import config

RESEARCH_STEPS = [
    {
        "id": "open_profile",
        "name": "1. Vào Hồ sơ",
        "desc": "Bấm vào tab Hồ sơ (góc dưới cùng bên phải màn hình)",
        "icon": "👤",
    },
    {
        "id": "swipe_showcase",
        "name": "2. Vào Phần trưng bày",
        "desc": "Vuốt qua trái để chuyển sang mục Phần trưng bày (Showcase)",
        "icon": "🛍️",
    },
    {
        "id": "tap_creator_shop",
        "name": "3. TikTok Shop cho nhà sáng tạo",
        "desc": "Bấm vào dòng 'TikTok Shop cho nhà sáng tạo >'",
        "icon": "🏪",
    },
    {
        "id": "tap_marketplace",
        "name": "4. Vào Sàn sản phẩm",
        "desc": "Bấm vào nút 'Sàn sản phẩm' để mở chợ tiếp thị liên kết",
        "icon": "🛒",
    },
    {
        "id": "tap_ranking_banner",
        "name": "5. Bấm Xếp hạng sản phẩm",
        "desc": "Bấm vào mục 'Xếp hạng sản phẩm' ở phần trên cùng",
        "icon": "🏆",
    },
    {
        "id": "select_niche",
        "name": "6. Chọn 1 ngách bất kỳ",
        "desc": "Chọn ngành hàng muốn làm (Đồ ăn nhẹ khô, Khăn giấy, Bánh quy, Mỹ phẩm...)",
        "icon": "🏷️",
    },
    {
        "id": "tap_see_more",
        "name": "7. Bấm Xem thêm",
        "desc": "Bấm 'Xem thêm' để kéo xem toàn bộ danh sách sản phẩm trong ngách",
        "icon": "📜",
    },
    {
        "id": "tap_product",
        "name": "8. Kiểm tra Giá & Đã bán",
        "desc": "Bấm vào từng sản phẩm: xem Giá tiền (rẻ là ưu tiên) và Đã bán (dưới 50k, đẹp nhất dưới 5k)",
        "icon": "🔍",
    },
    {
        "id": "expand_trend",
        "name": "9. Mở Xu hướng sản phẩm 7 ngày",
        "desc": "Vuốt nhẹ xuống hiện mũi tên ^, bấm vào xem: Đơn hàng vs Nhà sáng tạo (tỉ lệ 1:2 hoặc 1:3)",
        "icon": "📈",
    },
    {
        "id": "share_and_save",
        "name": "10. Lưu Link & Copy/Paste ID vào Phone",
        "desc": "Bấm nút Share -> Sao chép liên kết để tool tự bắt sản phẩm; kéo thả video để tự Copy & Paste ID vào máy",
        "icon": "💾",
    },
]


def parse_number(text: str | int | float | None) -> int:
    """Chuyển đổi chuỗi số TikTok ('58.8K', '2.4M', '43.000đ', '85.551₫ (-73%)', '9500') sang số nguyên."""
    if text is None:
        return 0
    if isinstance(text, (int, float)):
        return int(text)
    s = str(text).strip()
    if not s:
        return 0
    s = re.sub(r"\(.*?\)", "", s).strip()
    parts = re.split(r"[₫đ]|vnd|vnđ", s, flags=re.I)
    if parts and parts[0].strip():
        s = parts[0].strip()
    s = s.upper().replace(" ", "")
    if "K" in s:
        s = s.replace("K", "").replace(",", ".")
        try:
            return int(float(s) * 1000)
        except ValueError:
            return 0
    if "M" in s:
        s = s.replace("M", "").replace(",", ".")
        try:
            return int(float(s) * 1_000_000)
        except ValueError:
            return 0
    cleaned = re.sub(r"[^\d]", "", s)
    try:
        return int(cleaned) if cleaned else 0
    except ValueError:
        return 0


def extract_product_id(url_or_text: str | None) -> str:
    """Trích xuất ID sản phẩm từ URL chia sẻ hoặc chuỗi văn bản."""
    if not url_or_text:
        return ""
    text = str(url_or_text).strip()
    for pat in (
        r"product[/_]?id=([0-9]{12,25})",
        r"/pdp/([0-9]{12,25})",
        r"/product/([0-9]{12,25})",
        r"/products/([0-9]{12,25})",
        r"/p/([0-9]{12,25})",
        r"sku_id=([0-9]{12,25})",
    ):
        match = re.search(pat, text, re.IGNORECASE)
        if match:
            return match.group(1)
    match = re.search(r"\b([0-9]{15,22})\b", text)
    if match:
        return match.group(1)
    return ""


def _http_get_safe(url: str, **kwargs):
    import requests
    try:
        return requests.get(url, **kwargs)
    except Exception:
        with requests.Session() as direct_sess:
            direct_sess.trust_env = False
            return direct_sess.get(url, **kwargs)


def resolve_tiktok_share(raw_text: str, timeout: float = 4.5) -> dict[str, str]:
    """Phân tích chuỗi chia sẻ từ TikTok (hoặc link/ID sản phẩm).
    Tự động giải mã link rút gọn vt.tiktok.com / vm.tiktok.com để lấy ID và tiêu đề sản phẩm thật.
    """
    text = (raw_text or "").strip()
    if not text:
        return {"url": "", "product_id": "", "title": "", "raw": ""}

    url_match = re.search(r"https?://[^\s<>\"'()]+", text)
    found_url = url_match.group(0) if url_match else ""

    title = ""
    if found_url and not text.startswith(found_url):
        prefix = text[:text.find(found_url)].strip()
        prefix = re.sub(r"(?i)(hãy xem sản phẩm này trên tiktok shop|xem sản phẩm trên tiktok shop|tiktok shop).*", "", prefix).strip()
        prefix = re.sub(r"[!?:.\-_|]+$", "", prefix).strip()
        if len(prefix) >= 3:
            title = prefix

    product_id = extract_product_id(found_url or text)
    final_url = found_url

    is_short = bool(found_url and any(d in found_url.lower() for d in ("vt.tiktok.com", "vm.tiktok.com", "/t/")))
    if found_url and (is_short or not product_id):
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
                "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
            }
            resp = _http_get_safe(found_url, headers=headers, allow_redirects=True, timeout=timeout)
            if resp.url:
                final_url = resp.url
                extracted = extract_product_id(final_url)
                if extracted:
                    product_id = extracted

            if not title and resp.text:
                m_og = re.search(r'<meta[^>]+property=[\'"]og:title[\'"][^>]+content=[\'"]([^\'"]+)[\'"]', resp.text, re.I)
                if not m_og:
                    m_og = re.search(r'<meta[^>]+content=[\'"]([^\'"]+)[\'"][^>]+property=[\'"]og:title[\'"]', resp.text, re.I)
                if m_og:
                    t = m_og.group(1).strip()
                    t = re.sub(r"(?i)\s*[-|]\s*tiktok shop.*", "", t).strip()
                    if t and "not currently available" not in t.lower():
                        title = t
                else:
                    m_title = re.search(r"<title>([^<]+)</title>", resp.text, re.I)
                    if m_title:
                        t = m_title.group(1).strip()
                        t = re.sub(r"(?i)\s*[-|]\s*tiktok shop.*", "", t).strip()
                        if t and "not currently available" not in t.lower():
                            title = t
        except Exception:
            pass

    if not product_id:
        product_id = extract_product_id(text)

    if not title:
        title = f"Sản phẩm #{product_id}" if product_id else "Sản phẩm TikTok Shop"

    return {
        "url": final_url or found_url,
        "product_id": product_id,
        "title": title,
        "raw": text,
    }


def to_desktop_pdp_url(product_id_or_url: str | None) -> str:
    """Chuyển đổi ID sản phẩm hoặc link chia sẻ sang link PDP TikTok Shop."""
    if not product_id_or_url:
        return ""
    text = str(product_id_or_url).strip()
    pid = extract_product_id(text)
    if pid:
        return f"https://shop.tiktok.com/vn/pdp/{pid}"
    if text.startswith("http://") or text.startswith("https://"):
        resolved = resolve_tiktok_share(text)
        if resolved.get("product_id"):
            return f"https://shop.tiktok.com/vn/pdp/{resolved['product_id']}"
        return text
    return ""


def fetch_product_details_spaiauto(
    product_id_or_url: str,
    download_images: bool = True,
    allow_browser_fallback: bool = False,
    log_fn: Any = None,
) -> dict[str, Any]:
    """Cách 1 (Mặc định & Tối ưu nhất): Lấy toàn bộ thông tin & ảnh sản phẩm qua SPAI Auto API.
    - Hỗ trợ tự động thử lại với cả link PDP (`shop.tiktok.com/vn/pdp/{id}`) và link View Product (`shop.tiktok.com/view/product/{id}`).
    - Không mở trình duyệt Chrome tránh lỗi chặn vùng quốc gia của TikTok Web.
    """
    pdp_url = to_desktop_pdp_url(product_id_or_url)
    pid = extract_product_id(pdp_url) or extract_product_id(product_id_or_url)
    if not pdp_url and not pid:
        return {"success": False, "error": "Không tìm thấy ID sản phẩm hoặc URL hợp lệ."}
    if not pdp_url and pid:
        pdp_url = f"https://shop.tiktok.com/vn/pdp/{pid}"

    import requests

    api_url = "https://tools.spaiauto.com/api.php"
    headers = {
        "Accept": "*/*",
        "Accept-Language": "vi,en;q=0.9",
        "Content-Type": "application/json",
        "Origin": "https://tools.spaiauto.com",
        "Referer": "https://tools.spaiauto.com/lay-thong-tin-san-pham",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    }

    # Thử danh sách URL đầu vào cho SPAI Auto để tăng tỉ lệ thành công tối đa
    candidate_urls = [pdp_url]
    if pid:
        view_url = f"https://shop.tiktok.com/view/product/{pid}"
        if view_url not in candidate_urls:
            candidate_urls.append(view_url)

    spai_error = ""
    raw: dict[str, Any] = {}
    title = ""
    images: list[str] = []

    for cand_url in candidate_urls:
        payload = {
            "action": "extract_product",
            "url": cand_url,
            "recaptcha_token": "",
        }
        res_json: dict[str, Any] = {}
        try:
            resp = requests.post(api_url, json=payload, headers=headers, timeout=15)
            resp.raise_for_status()
            res_json = resp.json()
            spai_error = ""
        except Exception:
            try:
                # Dự phòng gọi trực tiếp bỏ qua Windows System Proxy (tránh lỗi khi PC còn lưu Proxy cũ đã tắt)
                with requests.Session() as direct_sess:
                    direct_sess.trust_env = False
                    resp = direct_sess.post(api_url, json=payload, headers=headers, timeout=15)
                    resp.raise_for_status()
                    res_json = resp.json()
                    spai_error = ""
            except Exception as e2:
                spai_error = f"Lỗi kết nối SPAI Auto: {e2}"
                continue

        if not res_json.get("success"):
            spai_error = res_json.get("message") or "SPAI Auto không trích xuất được sản phẩm."
            continue

        raw = res_json.get("data") or {}
        title = str(raw.get("title") or "").strip()
        images = [str(x) for x in raw.get("images", []) if x]
        if title or images:
            spai_error = ""
            break
        spai_error = "SPAI Auto trả về dữ liệu rỗng."

    if spai_error:
        if allow_browser_fallback:
            if callable(log_fn):
                log_fn(f"⚠️ Cách 1 (SPAI Auto) chưa phản hồi ({spai_error}) → Thử kiểm tra Cách 2 (Browser)...")
            browser_res = fetch_product_details_browser(
                product_id_or_url,
                download_images=download_images,
                fallback_to_spai=False,
                log_fn=log_fn,
            )
            if browser_res.get("success"):
                browser_res["fallback_from"] = "spaiauto"
                return browser_res
        return {"success": False, "error": spai_error}

    shop_name = str(raw.get("shop_name") or "").strip()
    price = parse_number(raw.get("price"))
    orig_price = parse_number(raw.get("original_price"))
    sold_count = parse_number(raw.get("sold_count"))
    sold_raw = str(raw.get("sold_count") or "").strip()
    desc = str(raw.get("description") or "").strip()
    desc_images = [str(x) for x in raw.get("description_images", []) if x]
    variations = raw.get("variations", [])
    primary_image = images[0] if images else ""

    local_thumb_path = ""
    local_images = []

    if download_images and pid:
        try:
            prod_img_dir = config.PRODUCT_IMAGES_DIR / pid
            prod_img_dir.mkdir(parents=True, exist_ok=True)

            if primary_image:
                thumb_file = prod_img_dir / "thumb.webp"
                if not thumb_file.is_file() or thumb_file.stat().st_size == 0:
                    img_resp = _http_get_safe(primary_image, timeout=15)
                    if img_resp.status_code == 200:
                        thumb_file.write_bytes(img_resp.content)
                if thumb_file.is_file():
                    local_thumb_path = f"product_images/{pid}/thumb.webp"

            images_subdir = prod_img_dir / "images"
            images_subdir.mkdir(parents=True, exist_ok=True)
            for idx, img_url in enumerate(images):
                img_path = images_subdir / f"img_{idx}.webp"
                if not img_path.is_file() or img_path.stat().st_size == 0:
                    try:
                        r = _http_get_safe(img_url, timeout=12)
                        if r.status_code == 200:
                            img_path.write_bytes(r.content)
                    except Exception:
                        pass
                if img_path.is_file():
                    local_images.append(str(img_path))

            info_file = prod_img_dir / "info.json"
            full_info = {
                "product_id": pid,
                "pdp_url": pdp_url,
                "title": title,
                "shop_name": shop_name,
                "price": price,
                "original_price": orig_price,
                "sold_count": sold_count,
                "sold_count_label": sold_raw,
                "description": desc,
                "images": images,
                "description_images": desc_images,
                "variations": variations,
                "local_images": local_images,
                "source": "spaiauto",
                "fetched_at": time.time(),
            }
            info_file.write_text(json.dumps(full_info, ensure_ascii=False, indent=2), encoding="utf-8")
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
        "sold_count_label": sold_raw,
        "description": desc,
        "images": images,
        "desc_images": desc_images,
        "variations": variations,
        "image_url": primary_image,
        "local_thumb": local_thumb_path,
        "total_images": len(images),
        "source": "spaiauto",
    }


def fetch_product_details_browser(
    product_id_or_url: str,
    download_images: bool = True,
    fallback_to_spai: bool = True,
    log_fn: Any = None,
) -> dict[str, Any]:
    """Cách 2: Thử tải từ Browser, NHƯNG nếu gặp lỗi chặn vùng quốc gia của TikTok
    ({"message":"The product is not currently available in your country or region."})
    hoặc bất kỳ lỗi nào thì TỰ ĐỘNG DÙNG CÁCH 1 (SPAI Auto) ngay lập tức!
    """
    from .browser_crawler import fetch_product_details_via_browser

    kwargs: dict[str, Any] = {"download_images": download_images}
    if callable(log_fn):
        kwargs["log_fn"] = log_fn
    res = fetch_product_details_via_browser(product_id_or_url, **kwargs)
    if res.get("success"):
        return res

    if fallback_to_spai:
        if callable(log_fn):
            log_fn(
                f"🔄 Cách 2 (Browser) lỗi ({res.get('error', 'Không rõ')}) "
                f"→ Tự động chuyển sang dùng Cách 1 (SPAI Auto)!"
            )
        spai_res = fetch_product_details_spaiauto(
            product_id_or_url,
            download_images=download_images,
            allow_browser_fallback=False,
            log_fn=log_fn,
        )
        if spai_res.get("success"):
            spai_res["fallback_from"] = "browser"
            return spai_res
        return {
            "success": False,
            "error": f"Cách 2 lỗi ({res.get('error')}) | Cách 1 lỗi ({spai_res.get('error')})",
        }
    return res


@dataclass
class ResearchProduct:
    id: str
    title: str = ""
    price: int = 0
    original_price: int = 0
    commission: int = 0
    sold_total: int = 0
    sold_total_label: str = ""
    orders_7d: int = 0
    creators_7d: int = 0
    ratio: float = 0.0
    ratio_label: str = ""
    status: str = "POTENTIAL"
    status_label: str = "Tiềm năng"
    share_url: str = ""
    pdp_url: str = ""
    image_url: str = ""
    local_thumb: str = ""
    images: list[str] = field(default_factory=list)
    desc_images: list[str] = field(default_factory=list)
    description: str = ""
    shop_name: str = ""
    variations: list[Any] = field(default_factory=list)
    has_details: bool = False
    category: str = ""
    rating: float = 0.0
    reviews_count: int = 0
    notes: str = ""
    is_hot: bool = False
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_product(
    price: int,
    sold_total: int,
    orders_7d: int,
    creators_7d: int,
    max_price: int = 200000,
    max_sold_total: int = 50000,
    min_ratio: float = 2.0,
    max_ratio: float = 9999.0,
) -> tuple[str, str, float, str]:
    creators = max(1, creators_7d)
    ratio = round(orders_7d / creators, 2)
    ratio_label = f"1 : {ratio}" if creators_7d > 0 else "—"

    pass_price = (price <= max_price) if price > 0 else True
    pass_sold = (sold_total <= max_sold_total) if sold_total > 0 else True
    pass_ratio = (ratio >= min_ratio) if creators_7d > 0 else False

    score = int(pass_price) + int(pass_sold) + int(pass_ratio)

    if score == 3:
        if sold_total <= 5000 and sold_total > 0:
            return "WIN", f"🏆 Cực phẩm WIN (3/3 tiêu chí, <5k đơn, tỉ lệ 1:{ratio})", ratio, ratio_label
        return "WIN", f"🎯 Sản phẩm WIN (3/3 tiêu chí, tỉ lệ 1:{ratio})", ratio, ratio_label

    if score == 2:
        reasons = []
        if pass_ratio:
            reasons.append(f"Tỉ lệ 1:{ratio} tốt")
        if pass_price:
            reasons.append("Giá hợp lý")
        if pass_sold:
            reasons.append(f"Đã bán ít ({sold_total:,})")
        return "WIN", f"✨ ĐẠT 2/3 tiêu chí ({', '.join(reasons)})", ratio, ratio_label

    if score == 1:
        if not pass_ratio and creators_7d >= 10:
            return "COMPETITIVE", f"Cạnh tranh cao (Nhiều creator làm mà ít đơn, tỉ lệ 1:{ratio})", ratio, ratio_label
        if not pass_sold:
            return "SATURATED", f"Đã bán quá {max_sold_total:,} đơn (bão hòa)", ratio, ratio_label
        return "POTENTIAL", "Chỉ đạt 1/3 tiêu chí (Cần theo dõi thêm)", ratio, ratio_label

    return "SATURATED", "Không đạt tiêu chí (Giá cao, đã bán nhiều, tỉ lệ thấp)", ratio, ratio_label


class ResearchStore:
    def __init__(self, file_path: Path | None = None):
        self.file_path = Path(file_path or config.STORE_RESEARCH)
        self.lock = threading.RLock()
        self._cache: dict[str, ResearchProduct] = {}
        self._last_mtime: float = 0.0
        self.criteria: dict[str, Any] = {
            "maxPrice": 200000,
            "maxSoldTotal": 50000,
            "bestSoldTotal": 5000,
            "minRatio": 2.0,
            "maxRatio": 9999.0,
            "targetCategory": "",
            "autoScanCount": 20,
        }
        self.on_update = None
        self.load()

    def load(self) -> None:
        with self.lock:
            if not self.file_path.is_file():
                self._cache = {}
                return
            try:
                self._last_mtime = self.file_path.stat().st_mtime
                data = json.loads(self.file_path.read_text(encoding="utf-8"))
                products = data.get("products", [])
                self.criteria.update(data.get("criteria", {}))
                self._cache = {}
                for p in products:
                    if isinstance(p, dict) and p.get("id"):
                        prod = ResearchProduct(**{k: v for k, v in p.items() if k in ResearchProduct.__annotations__})
                        self._cache[prod.id] = prod
            except Exception:
                self._cache = {}

    def _create_backup(self) -> None:
        try:
            if not self._cache:
                return
            backup_dir = self.file_path.parent / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_file = backup_dir / "tiktok-product-research-backup.json"
            payload = {
                "criteria": self.criteria,
                "updatedAt": time.time(),
                "products": [p.to_dict() for p in self._cache.values()],
            }
            backup_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def save(self, create_backup: bool = True) -> None:
        with self.lock:
            try:
                if create_backup and self._cache:
                    self._create_backup()
                self.file_path.parent.mkdir(parents=True, exist_ok=True)
                payload = {
                    "criteria": self.criteria,
                    "updatedAt": time.time(),
                    "products": [p.to_dict() for p in self._cache.values()],
                }
                self.file_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                self._last_mtime = self.file_path.stat().st_mtime
            except Exception:
                pass

    def _ensure_fresh(self) -> None:
        try:
            if self.file_path.is_file() and self.file_path.stat().st_mtime > self._last_mtime:
                self.load()
        except Exception:
            pass

    def list_products(self) -> list[dict[str, Any]]:
        with self.lock:
            self._ensure_fresh()
            items = sorted(self._cache.values(), key=lambda x: (x.status == "WIN", x.created_at), reverse=True)
            return [p.to_dict() for p in items]

    def add_or_update(self, data: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self._ensure_fresh()
            prod_id = str(data.get("id") or "").strip()
            if not prod_id:
                prod_id = extract_product_id(data.get("share_url") or data.get("url") or "")
            if not prod_id:
                prod_id = f"prod_{int(time.time() * 1000)}"

            existing = self._cache.get(prod_id)

            price = parse_number(data.get("price")) if "price" in data else (existing.price if existing else 0)
            orig_price = parse_number(data.get("original_price")) if "original_price" in data else (existing.original_price if existing else 0)
            sold_total = parse_number(data.get("sold_total") or data.get("soldTotal")) if ("sold_total" in data or "soldTotal" in data) else (existing.sold_total if existing else 0)
            orders_7d = parse_number(data.get("orders_7d") or data.get("orders7d")) if ("orders_7d" in data or "orders7d" in data) else (existing.orders_7d if existing else 0)
            creators_7d = parse_number(data.get("creators_7d") or data.get("creators7d")) if ("creators_7d" in data or "creators7d" in data) else (existing.creators_7d if existing else 0)

            status, status_label, ratio, ratio_label = evaluate_product(
                price=price,
                sold_total=sold_total,
                orders_7d=orders_7d,
                creators_7d=creators_7d,
                max_price=int(self.criteria.get("maxPrice", 200000)),
                max_sold_total=int(self.criteria.get("maxSoldTotal", 50000)),
                min_ratio=float(self.criteria.get("minRatio", 2.0)),
                max_ratio=float(self.criteria.get("maxRatio", 9999.0)),
            )

            title = str(data.get("title") or (existing.title if existing else "") or "Sản phẩm TikTok Shop").strip()
            share_url = str(data.get("share_url") or data.get("url") or (existing.share_url if existing else "")).strip()
            pdp_url = str(data.get("pdp_url") or (existing.pdp_url if existing else "") or to_desktop_pdp_url(share_url or prod_id)).strip()
            image_url = str(data.get("image_url") or (existing.image_url if existing else "")).strip()
            local_thumb = str(data.get("local_thumb") or (existing.local_thumb if existing else "")).strip()
            images = list(data.get("images") or (existing.images if existing else []))
            desc_images = list(data.get("desc_images") or (existing.desc_images if existing else []))
            description = str(data.get("description") or (existing.description if existing else "")).strip()
            shop_name = str(data.get("shop_name") or (existing.shop_name if existing else "")).strip()
            variations = list(data.get("variations") or (existing.variations if existing else []))
            has_details = bool(data.get("has_details", (existing.has_details if existing else False)))
            is_hot = bool(data.get("is_hot", (existing.is_hot if existing else False)))

            product = ResearchProduct(
                id=prod_id,
                title=title,
                price=price,
                original_price=orig_price,
                commission=parse_number(data.get("commission") if "commission" in data else (existing.commission if existing else 0)),
                sold_total=sold_total,
                sold_total_label=str(data.get("sold_total_label") or (existing.sold_total_label if existing else "") or (f"{sold_total:,}" if sold_total else "")),
                orders_7d=orders_7d,
                creators_7d=creators_7d,
                ratio=ratio,
                ratio_label=ratio_label,
                status=status,
                status_label=status_label,
                share_url=share_url,
                pdp_url=pdp_url,
                image_url=image_url,
                local_thumb=local_thumb,
                images=images,
                desc_images=desc_images,
                description=description,
                shop_name=shop_name,
                variations=variations,
                has_details=has_details,
                category=str(data.get("category") or (existing.category if existing else "") or self.criteria.get("targetCategory") or "").strip(),
                rating=float(data.get("rating") if "rating" in data else (existing.rating if existing else 0.0)),
                reviews_count=int(data.get("reviews_count") if "reviews_count" in data else (existing.reviews_count if existing else 0)),
                notes=str(data.get("notes") or (existing.notes if existing else "")).strip(),
                is_hot=is_hot,
                created_at=float(data.get("created_at") or (existing.created_at if existing else time.time())),
            )
            self._cache[product.id] = product
            if product.is_hot:
                try:
                    self._sync_hot_folder(product)
                except Exception:
                    pass
        self.save()
        return product.to_dict()

    def enrich_product(
        self,
        product_id: str,
        download_images: bool = True,
        method: str = "spaiauto",
        log_fn: Any = None,
    ) -> dict[str, Any] | None:
        """Lấy thông tin chi tiết và ảnh sản phẩm:
        - method='spaiauto' / 'auto' (Mặc định): Dùng Cách 1 (SPAI Auto) trực tiếp, nhanh và không bật Chrome.
        - method='browser': Thử Cách 2 (Browser), nếu lỗi (VD: TikTok báo 'not currently available in your country or region')
          thì TỰ ĐỘNG DÙNG CÁCH 1 (SPAI Auto) luôn.
        """
        with self.lock:
            self._ensure_fresh()
            p = self._cache.get(product_id)
            if not p:
                return None
            target = p.pdp_url or p.share_url or p.id

        mode = (method or "spaiauto").strip().lower()
        if mode == "browser":
            details = fetch_product_details_browser(
                target,
                download_images=download_images,
                fallback_to_spai=True,
                log_fn=log_fn,
            )
        else:
            # Ưu tiên tuyệt đối Cách 1 (SPAI Auto) — không tự bật Chrome gây lỗi region
            details = fetch_product_details_spaiauto(
                target,
                download_images=download_images,
                allow_browser_fallback=False,
                log_fn=log_fn,
            )

        if not details.get("success"):
            return None

        with self.lock:
            self._ensure_fresh()
            p = self._cache.get(product_id)
            if not p:
                return None

            if details.get("title") and (
                not p.title
                or p.title.startswith("Sản phẩm #")
                or p.title.strip().lower() in ("security check", "tiktok", "tiktok shop", "verify")
                or not p.has_details
                or len(details["title"]) >= len(p.title)
            ):
                p.title = details["title"]

            if details.get("shop_name"):
                p.shop_name = details["shop_name"]
            if details.get("price") and (p.price == 0 or details["price"] > 0):
                p.price = details["price"]
            if details.get("original_price"):
                p.original_price = details["original_price"]
            if details.get("sold_count") and (p.sold_total == 0 or details["sold_count"] > 0):
                p.sold_total = details["sold_count"]
                if details.get("sold_count_label"):
                    p.sold_total_label = details["sold_count_label"]
            if details.get("description"):
                p.description = details["description"]
            if details.get("images"):
                p.images = details["images"]
            if details.get("desc_images"):
                p.desc_images = details["desc_images"]
            if details.get("variations"):
                p.variations = details["variations"]
            if details.get("image_url"):
                p.image_url = details["image_url"]
            if details.get("local_thumb"):
                p.local_thumb = details["local_thumb"]
            if details.get("pdp_url"):
                p.pdp_url = details["pdp_url"]

            p.has_details = True

            status, status_label, ratio, ratio_label = evaluate_product(
                price=p.price,
                sold_total=p.sold_total,
                orders_7d=p.orders_7d,
                creators_7d=p.creators_7d,
                max_price=int(self.criteria.get("maxPrice", 200000)),
                max_sold_total=int(self.criteria.get("maxSoldTotal", 50000)),
                min_ratio=float(self.criteria.get("minRatio", 2.0)),
                max_ratio=float(self.criteria.get("maxRatio", 9999.0)),
            )
            p.status = status
            p.status_label = status_label
            p.ratio = ratio
            p.ratio_label = ratio_label

            if p.is_hot:
                try:
                    self._sync_hot_folder(p)
                except Exception:
                    pass

            self.save(create_backup=False)
            out_dict = p.to_dict()
            out_dict["fetch_source"] = details.get("source", "spaiauto")
            out_dict["fallback_from"] = details.get("fallback_from", "")
            return out_dict

    def _sync_hot_folder(self, product: ResearchProduct) -> None:
        hot_dir = config.HOT_PRODUCTS_DIR / product.id
        if not product.is_hot:
            if hot_dir.exists():
                shutil.rmtree(hot_dir, ignore_errors=True)
            return

        hot_dir.mkdir(parents=True, exist_ok=True)
        info_path = hot_dir / "info.json"
        info_path.write_text(json.dumps(product.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

        txt_path = hot_dir / "thong_tin_san_pham.txt"
        created_str = time.strftime("%d/%m/%Y %H:%M:%S", time.localtime(product.created_at)) if product.created_at else ""
        lines = [
            "=" * 60,
            f"🔥 SẢN PHẨM HOT TIKTOK SHOP: {product.title}",
            "=" * 60,
            f"ID Sản phẩm      : {product.id}",
            f"Tên sản phẩm     : {product.title}",
            f"Cửa hàng         : {product.shop_name or 'Chưa có'}",
            f"Giá bán          : {product.price:,} đ" if product.price > 0 else "Giá bán          : Chưa có",
            f"Giá gốc          : {product.original_price:,} đ" if product.original_price > 0 else "",
            f"Đã bán           : {product.sold_total:,} ({product.sold_total_label})" if product.sold_total > 0 else f"Đã bán           : {product.sold_total_label or '0'}",
            f"Đánh giá tiêu chí: {product.status_label}",
            f"Link Web PDP     : {product.pdp_url}",
            f"Link Chia sẻ     : {product.share_url}",
            f"Ngày thêm        : {created_str}",
            "",
            "---------------- MÔ TẢ CHI TIẾT SẢN PHẨM ----------------",
            product.description or "Chưa có nội dung mô tả chi tiết.",
            "",
            f"---------------- LINK ẢNH SẢN PHẨM ({len(product.images)} ảnh) ----------------",
            "\n".join(product.images) if product.images else (product.image_url or "Chưa có link ảnh"),
        ]
        if product.desc_images:
            lines.extend([
                "",
                f"---------------- LINK ẢNH TRONG MÔ TẢ ({len(product.desc_images)} ảnh) ----------------",
                "\n".join(product.desc_images),
            ])
        txt_path.write_text("\n".join(line for line in lines if line is not None), encoding="utf-8")

        pdp_target = product.pdp_url or product.share_url or (f"https://shop.tiktok.com/vn/pdp/{product.id}" if product.id else "")
        if pdp_target:
            url_file = hot_dir / "Mo_Tren_Web.url"
            url_file.write_text(f"[InternetShortcut]\nURL={pdp_target}\n", encoding="utf-8")

        src_dir = config.PRODUCT_IMAGES_DIR / product.id
        if src_dir.is_dir():
            src_thumb = src_dir / "thumb.webp"
            if src_thumb.is_file():
                shutil.copy2(src_thumb, hot_dir / "thumb.webp")
            src_imgs = src_dir / "images"
            if src_imgs.is_dir():
                dst_imgs = hot_dir / "images"
                dst_imgs.mkdir(parents=True, exist_ok=True)
                for f in src_imgs.iterdir():
                    if f.is_file():
                        shutil.copy2(f, dst_imgs / f.name)
        elif product.image_url:
            hot_thumb = hot_dir / "thumb.webp"
            if not hot_thumb.exists() or hot_thumb.stat().st_size == 0:
                try:
                    import requests
                    r = requests.get(product.image_url, timeout=10)
                    if r.status_code == 200:
                        hot_thumb.write_bytes(r.content)
                except Exception:
                    pass

    def _auto_enrich_hot(self, product_id: str) -> None:
        try:
            self.enrich_product(product_id, download_images=True, method="spaiauto")
            if callable(self.on_update):
                self.on_update()
        except Exception:
            pass

    def toggle_hot(self, product_id: str, auto_enrich: bool = True) -> bool:
        with self.lock:
            p = self._cache.get(product_id)
            if not p:
                return False
            p.is_hot = not p.is_hot
            is_hot = p.is_hot
            needs_enrich = is_hot and not p.has_details
            try:
                self._sync_hot_folder(p)
            except Exception:
                pass
            self.save(create_backup=False)

        if needs_enrich and auto_enrich:
            threading.Thread(
                target=self._auto_enrich_hot,
                args=(product_id,),
                daemon=True,
                name=f"enrich-hot-{product_id}",
            ).start()
        return is_hot

    def set_hot(self, product_id: str, hot: bool = True, auto_enrich: bool = True) -> bool:
        with self.lock:
            p = self._cache.get(product_id)
            if not p:
                return False
            p.is_hot = bool(hot)
            is_hot = p.is_hot
            needs_enrich = is_hot and not p.has_details
            try:
                self._sync_hot_folder(p)
            except Exception:
                pass
            self.save(create_backup=False)

        if needs_enrich and auto_enrich:
            threading.Thread(
                target=self._auto_enrich_hot,
                args=(product_id,),
                daemon=True,
                name=f"enrich-hot-{product_id}",
            ).start()
        return is_hot

    def sync_all_hot(self) -> int:
        count = 0
        with self.lock:
            hot_prods = [p for p in self._cache.values() if p.is_hot]
        for p in hot_prods:
            try:
                self._sync_hot_folder(p)
                count += 1
            except Exception:
                pass
        return count

    def remove(self, product_id: str) -> bool:
        with self.lock:
            if product_id in self._cache:
                p = self._cache.pop(product_id)
                if p.is_hot:
                    hot_dir = config.HOT_PRODUCTS_DIR / product_id
                    if hot_dir.exists():
                        shutil.rmtree(hot_dir, ignore_errors=True)
                self.save()
                return True
        return False

    def get_product(self, product_id: str) -> dict[str, Any] | None:
        with self.lock:
            p = self._cache.get(product_id)
            return p.to_dict() if p else None

    def check_duplicate(self, raw_text_or_id: str) -> dict[str, Any] | None:
        text = (raw_text_or_id or "").strip()
        if not text:
            return None
        resolved = resolve_tiktok_share(text)
        prod_id = resolved.get("product_id") or extract_product_id(text)
        url = resolved.get("url") or ""

        with self.lock:
            if prod_id and prod_id in self._cache:
                return self._cache[prod_id].to_dict()
            if url:
                clean_url = url.split("?")[0].rstrip("/")
                for p in self._cache.values():
                    if p.share_url:
                        p_clean = p.share_url.split("?")[0].rstrip("/")
                        if p_clean == clean_url:
                            return p.to_dict()
            if text in self._cache:
                return self._cache[text].to_dict()
        return None

    def add_from_clipboard(self, raw_text: str, source: str = "phone", auto_enrich: bool = False) -> dict[str, Any] | None:
        resolved = resolve_tiktok_share(raw_text)
        prod_id = resolved.get("product_id") or ""
        url = resolved.get("url") or ""
        title = resolved.get("title") or "Sản phẩm TikTok Shop"
        if not prod_id and not url:
            return None

        with self.lock:
            existing = self._cache.get(prod_id) if prod_id else None
            data: dict[str, Any] = {
                "id": prod_id or f"prod_{int(time.time() * 1000)}",
                "title": title if (not existing or existing.title.startswith("Sản phẩm #")) else existing.title,
                "share_url": url or (existing.share_url if existing else ""),
                "pdp_url": to_desktop_pdp_url(url or prod_id),
                "notes": f"Tự động thêm từ {source}",
            }
            if existing:
                for field_name in (
                    "price", "original_price", "commission", "sold_total", "sold_total_label",
                    "orders_7d", "creators_7d", "category", "rating", "reviews_count",
                    "image_url", "local_thumb", "images", "desc_images", "description",
                    "shop_name", "variations", "has_details", "is_hot",
                ):
                    data[field_name] = getattr(existing, field_name)

        prod = self.add_or_update(data)

        if auto_enrich and prod_id and not prod.get("has_details"):
            def _bg_enrich():
                try:
                    time.sleep(0.3)
                    self.enrich_product(prod_id, download_images=True, method="spaiauto")
                    if callable(self.on_update):
                        self.on_update()
                except Exception:
                    pass
            threading.Thread(target=_bg_enrich, daemon=True, name=f"enrich-{prod_id}").start()

        return prod

    def batch_add_text(self, multiline_text: str, source: str = "Dán danh sách") -> dict[str, Any]:
        lines = [line.strip() for line in (multiline_text or "").splitlines() if line.strip()]
        added = []
        duplicates = []
        for line in lines:
            existing = self.check_duplicate(line)
            if existing:
                duplicates.append(existing)
                continue
            prod = self.add_from_clipboard(line, source=source, auto_enrich=False)
            if prod:
                added.append(prod)
        return {
            "added": len(added),
            "duplicates": len(duplicates),
            "duplicate_items": duplicates,
            "products": added,
        }

    def import_from_json_path(self, json_file: Path, src_images_dir: Path | None = None) -> dict[str, int]:
        """Nhập sản phẩm từ file JSON (ví dụ kho sản phẩm của tool cũ) không ghi đè sản phẩm đã có."""
        if not json_file.is_file():
            raise FileNotFoundError(f"Không tìm thấy file dữ liệu: {json_file}")
        raw = json.loads(json_file.read_text(encoding="utf-8"))
        items = raw.get("products", []) if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
        added = 0
        skipped = 0
        with self.lock:
            for item in items:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                pid = str(item["id"]).strip()
                if pid in self._cache:
                    skipped += 1
                    continue
                prod = ResearchProduct(**{k: v for k, v in item.items() if k in ResearchProduct.__annotations__})
                self._cache[pid] = prod
                added += 1
                if src_images_dir and (src_images_dir / pid).is_dir():
                    dst_dir = config.PRODUCT_IMAGES_DIR / pid
                    if not dst_dir.exists():
                        try:
                            shutil.copytree(src_images_dir / pid, dst_dir)
                        except Exception:
                            pass
                if prod.is_hot:
                    try:
                        self._sync_hot_folder(prod)
                    except Exception:
                        pass
            if added > 0:
                self.save()
        return {"added": added, "skipped": skipped, "total": len(self._cache)}

    def remove_multiple(self, product_ids: list[str]) -> int:
        with self.lock:
            if not product_ids:
                return 0
            if self._cache:
                self._create_backup()
            count = 0
            for pid in product_ids:
                p_str = str(pid).strip()
                if p_str in self._cache:
                    p = self._cache.pop(p_str)
                    if p.is_hot:
                        shutil.rmtree(config.HOT_PRODUCTS_DIR / p_str, ignore_errors=True)
                    count += 1
            if count > 0:
                self.save(create_backup=False)
            return count

    def clear(self) -> None:
        with self.lock:
            if self._cache:
                self._create_backup()
            self._cache.clear()
            self.save(create_backup=False)

    def restore_backup(self) -> int:
        with self.lock:
            backup_file = self.file_path.parent / "backups" / "tiktok-product-research-backup.json"
            if not backup_file.is_file():
                return 0
            try:
                data = json.loads(backup_file.read_text(encoding="utf-8"))
                products = data.get("products", [])
                self._cache = {}
                for p in products:
                    if isinstance(p, dict) and p.get("id"):
                        prod = ResearchProduct(**{k: v for k, v in p.items() if k in ResearchProduct.__annotations__})
                        self._cache[prod.id] = prod
                self.save(create_backup=False)
                return len(self._cache)
            except Exception:
                return 0

    def export_excel(self) -> bytes:
        import datetime
        import openpyxl
        from openpyxl.styles import Alignment, Font, PatternFill

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "San Pham TikTok Shop"

        headers = [
            "STT",
            "HOT",
            "ID sản phẩm",
            "Tên sản phẩm",
            "Cửa hàng",
            "Giá bán (VNĐ)",
            "Đã bán",
            "Ngành hàng",
            "Link TikTok Shop",
            "Thời gian lưu",
            "Nguồn / Ghi chú",
        ]
        ws.append(headers)

        header_fill = PatternFill(start_color="1F2937", end_color="1F2937", fill_type="solid")
        header_font = Font(name="Segoe UI", size=11, bold=True, color="FFFFFF")

        for col_idx in range(1, len(headers) + 1):
            cell = ws.cell(row=1, column=col_idx)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center")

        with self.lock:
            items = sorted(self._cache.values(), key=lambda x: x.created_at, reverse=True)
            for idx, p in enumerate(items, 1):
                try:
                    dt = datetime.datetime.fromtimestamp(p.created_at)
                    date_str = dt.strftime("%d/%m/%Y %H:%M")
                except Exception:
                    date_str = ""

                row = [
                    idx,
                    "HOT" if p.is_hot else "",
                    str(p.id),
                    p.title,
                    p.shop_name,
                    p.price if p.price > 0 else "",
                    p.sold_total if p.sold_total > 0 else p.sold_total_label,
                    p.category,
                    p.share_url or p.pdp_url,
                    date_str,
                    p.notes,
                ]
                ws.append(row)
                row_idx = ws.max_row
                ws.cell(row=row_idx, column=1).alignment = Alignment(horizontal="center")
                ws.cell(row=row_idx, column=2).alignment = Alignment(horizontal="center")
                ws.cell(row=row_idx, column=3).number_format = "@"

        for col in ws.columns:
            max_len = max(len(str(cell.value or "")) for cell in col)
            col_letter = openpyxl.utils.get_column_letter(col[0].column)
            ws.column_dimensions[col_letter].width = min(45, max(12, max_len + 3))

        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()
