from hoshino import Service, priv
from hoshino.typing import CQEvent
from hoshino.util import DailyNumberLimiter, FreqLimiter, escape
import json
import os
import asyncio
import aiohttp
import re
import html
import random
import time
import math
import base64
from io import BytesIO
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote
from PIL import Image, ImageOps
import hoshino


class WeiboAuthError(Exception):
    """微博认证类错误基类"""


class CookieExpiredError(WeiboAuthError):
    """微博 Cookie 已失效，且自动续期失败"""


class LoginRequiredError(WeiboAuthError):
    """该接口需要真实登录，游客身份不可用"""


_cookie_expired_notified = False
  
sv = Service('微博推送', visible=True, enable_on_default=False, help_='微博推送服务')  
  
# 定义数据文件路径  
DATA_FILE = os.path.join(os.path.dirname(__file__), 'data.json')  
# 配置文件路径  
CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'weibo_config.json')  
  
# 频率限制 - CD冷却10秒，每天20000次  
flmt = FreqLimiter(10)  
_nlmt = DailyNumberLimiter(20000)  
  
# 配置结构：群独立黑名单  
weibo_config = {  
    'group_follows': {},      # {group_id: {weibo_id: {name: '微博名', last_post_time: '2024-01-01 12:00:00'}}}  
    'group_enable': {},       # {group_id: True/False}  
    'account_cache': {},      # {weibo_id: {name: '微博名', uid: '微博ID'}}  
    'group_blacklist': {}     # {group_id: set(weibo_id)} 群独立黑名单  
} 
  
def _resolve_month_day(now, month, day, hour, minute):
    """由月/日推断年份：默认今年；若算出未来时间超过 1 天，则视为去年（跨年场景）"""
    for year in (now.year, now.year - 1):
        try:
            dt = datetime(year, month, day, hour, int(minute))
        except ValueError:
            return ''
        if dt - now <= timedelta(days=1):
            return dt.strftime('%Y-%m-%d %H:%M:%S')
    return ''


def format_weibo_time(time_text):
    """把微博时间文本标准化为本地时间 YYYY-MM-DD HH:MM:SS

    m.weibo.cn 的 created_at 有多种形态：
      "Thu Oct 31 11:03:09 +0800 2026"（API 原始格式，最常见）
      "刚刚" / "3分钟前" / "1小时前" / "今天 10:30" / "昨天 10:30"
      "10-30" / "10-30 10:30"（24 小时以前的微博）
      "10月30日 10:30" / "2026-10-30 10:30:00"
    统一转成绝对时间用于比较；实在解析不了返回空串（绝不返回原文本，
    否则会污染 last_post_time 的字符串比较）。
    """
    if not time_text:
        return ''
    text = str(time_text).strip()
    if text in ('unknown', '未知', 'Invalid Date', 'None'):
        return ''

    now = datetime.now()
    try:
        # API 原始格式：Thu Oct 31 11:03:09 +0800 2026
        try:
            dt = datetime.strptime(text, '%a %b %d %H:%M:%S %z %Y')
            return dt.astimezone().strftime('%Y-%m-%d %H:%M:%S')
        except (ValueError, AttributeError):
            pass

        # 绝对时间：YYYY-MM-DD[ HH:MM[:SS]]
        m = re.match(r'^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?$', text)
        if m:
            return (f"{m.group(1)}-{m.group(2)}-{m.group(3)} "
                    f"{m.group(4) or '00'}:{m.group(5) or '00'}:{m.group(6) or '00'}")

        # 刚刚 / X秒前
        if text.startswith('刚刚'):
            return now.strftime('%Y-%m-%d %H:%M:%S')
        m = re.search(r'(\d+)\s*秒前', text)
        if m:
            return (now - timedelta(seconds=int(m.group(1)))).strftime('%Y-%m-%d %H:%M:%S')

        # X分钟前 / X小时前
        m = re.search(r'(\d+)\s*分钟前', text)
        if m:
            return (now - timedelta(minutes=int(m.group(1)))).strftime('%Y-%m-%d %H:%M:%S')
        m = re.search(r'(\d+)\s*小时前', text)
        if m:
            return (now - timedelta(hours=int(m.group(1)))).strftime('%Y-%m-%d %H:%M:%S')

        # 今天 HH:MM / 昨天 HH:MM
        m = re.search(r'今天\s*(\d{1,2}):(\d{2})', text)
        if m:
            return f"{now.strftime('%Y-%m-%d')} {int(m.group(1)):02d}:{m.group(2)}:00"
        m = re.search(r'昨天\s*(\d{1,2}):(\d{2})', text)
        if m:
            day = now - timedelta(days=1)
            return f"{day.strftime('%Y-%m-%d')} {int(m.group(1)):02d}:{m.group(2)}:00"

        # M月D日 [HH:MM]
        m = re.search(r'(\d{1,2})月(\d{1,2})日(?:\s*(\d{1,2}):(\d{2}))?', text)
        if m:
            return _resolve_month_day(now, int(m.group(1)), int(m.group(2)),
                                      int(m.group(3) or 0), m.group(4) or '00')

        # MM-DD [HH:MM]（旧版本里这类时间会被当成 "10-30 00:00:00"，
        # 字符串比较永远小于 "2026-..."，导致跨天补推彻底失效）
        m = re.match(r'^(\d{1,2})-(\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?$', text)
        if m:
            return _resolve_month_day(now, int(m.group(1)), int(m.group(2)),
                                      int(m.group(3) or 0), m.group(4) or '00')
    except Exception as e:
        sv.logger.warning(f"时间格式化失败: {time_text}, 错误: {e}")
    return ''


        
  
def _atomic_write_json(path: str, payload) -> None:
    """原子写 JSON：先写临时文件再替换，避免写一半崩溃把文件写坏"""
    tmp_path = f'{path}.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def _load_json_file(path: str, default):
    """读取 JSON；损坏时先备份原文件再返回默认值（绝不静默清空数据）"""
    if not os.path.exists(path):
        return default
    try:
        with open(path, 'r', encoding='utf-8') as f:
            loaded = json.load(f)
        return loaded if isinstance(loaded, dict) else default
    except Exception as e:
        backup = f'{path}.bad-{datetime.now().strftime("%Y%m%d%H%M%S")}'
        try:
            os.replace(path, backup)
            sv.logger.error(f"数据文件损坏({e})，已备份为 {backup}")
        except Exception:
            sv.logger.error(f"数据文件损坏({e})，且备份失败")
        return default


def load_config():
    """加载配置文件（带向后兼容）"""
    global weibo_config
    loaded_config = _load_json_file(CONFIG_PATH, None)
    if loaded_config is None:
        save_config()
        return

    for key in ['group_follows', 'group_enable', 'account_cache']:
        weibo_config[key] = loaded_config.get(key) or {}

    # 迁移：把旧版的 last_post_id 换成 last_post_time
    for follows in weibo_config['group_follows'].values():
        for info in follows.values():
            if 'last_post_id' in info and 'last_post_time' not in info:
                info['last_post_time'] = ''
                del info['last_post_id']

    weibo_config['group_blacklist'] = {}
    for group_id, uids in (loaded_config.get('group_blacklist') or {}).items():
        weibo_config['group_blacklist'][group_id] = set(uids)


def save_config():
    """保存配置文件（集合转列表以便 JSON 序列化，并原子落盘）"""
    config_to_save = dict(weibo_config)
    config_to_save['group_blacklist'] = {
        group_id: sorted(uids) for group_id, uids in weibo_config['group_blacklist'].items()
    }
    try:
        _atomic_write_json(CONFIG_PATH, config_to_save)
    except Exception as e:
        sv.logger.error(f"保存配置失败: {e}")


  
# ============================================================================
# Cookie 生命周期管理
# ----------------------------------------------------------------------------
# 问题：m.weibo.cn 的 getIndex 接口必须有 .weibo.cn 域的 SUB Cookie；从浏览器
#       手工复制的登录 Cookie 通常 7 天左右就失效，需要人工重新复制，很折腾。
#
# 方案：
#   1) 自动申请「游客身份」Cookie —— 走新浪 h5 官方访客流程
#      (genvisitor2 + crossdomain)，不需要账号密码，签发的是 .weibo.cn 域 SUB，
#      有效期约一年，失效后随时可以重新申请。默认走这条路，基本无需人工干预。
#   2) 手工 Cookie 依然支持且优先使用（它能翻更多页），一旦失效自动降级到游客
#      身份并记录状态，机器人不会因为 Cookie 过期而停摆。
#   3) 每次响应里的 Set-Cookie 都滚动合并回本地（SUB/_T_WM/XSRF-TOKEN 等），
#      保持身份新鲜，降低被风控的概率。
# ============================================================================

API_BASE = 'https://m.weibo.cn'
_VISITOR_HOME = 'https://visitor.passport.weibo.cn'
_VISITOR_ENTRY = 'sinawap'
_VISITOR_VER = '20250916'
_MOBILE_UA = ('Mozilla/5.0 (Linux; Android 13; Mobile) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36')
# 只接受这些域下的 Cookie：新浪在 .weibo.com 域签发的 SUB 会被 m.weibo.cn
# 主动删除并跳登录页，混进 Cookie 头只会帮倒忙。
_CN_COOKIE_DOMAINS = ('.weibo.cn', 'weibo.cn', 'm.weibo.cn', '.m.weibo.cn')
_COOKIE_ATTRS = {'path', 'domain', 'expires', 'max-age', 'samesite', 'secure',
                 'httponly', 'priority', 'comment', 'version'}

_BASE_HEADERS = {
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'User-Agent': _MOBILE_UA,
    'Connection': 'keep-alive',
}

_session = None
_session_loop = None


async def _get_session() -> aiohttp.ClientSession:
    """全局复用的 aiohttp 会话：连接池复用，不再每个请求重新握手"""
    global _session, _session_loop
    loop = asyncio.get_event_loop()
    if _session is None or _session.closed or _session_loop is not loop:
        _session = aiohttp.ClientSession(
            headers=_BASE_HEADERS,
            cookie_jar=aiohttp.DummyCookieJar(),  # Cookie 全部自己管，行为可预期
            connector=aiohttp.TCPConnector(limit=16, ttl_dns_cache=600),
        )
        _session_loop = loop
    return _session


def _now_str() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


_MONTH_MAP = {name: idx for idx, name in enumerate(
    ['jan', 'feb', 'mar', 'apr', 'may', 'jun',
     'jul', 'aug', 'sep', 'oct', 'nov', 'dec'], start=1)}
# 新浪的 Set-Cookie 用的是非标准日期："Sunday, 31-Oct-2027 03:04:33 GMT"
# （日-月-年用连字符），email.utils 解析不了，需要自己兜底。
_HTTP_DATE_RE = re.compile(r'(\d{1,2})[- ]([A-Za-z]{3})[a-z]*[- ](\d{4})\D+(\d{1,2}):(\d{2}):(\d{2})')


def _parse_http_date(value: str):
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except Exception:
        pass
    m = _HTTP_DATE_RE.search(value)
    if not m:
        return None
    day, mon, year, hour, minute, second = m.groups()
    month = _MONTH_MAP.get(mon.lower())
    if not month:
        return None
    try:
        return datetime(int(year), month, int(day), int(hour), int(minute), int(second),
                        tzinfo=timezone.utc)
    except ValueError:
        return None


def _to_local_str(value: str) -> str:
    dt = _parse_http_date(value)
    return dt.astimezone().strftime('%Y-%m-%d %H:%M:%S') if dt else ''


def normalize_cookie_text(text: str) -> str:
    """把从浏览器/聊天窗口复制来的 Cookie 文本整理成标准串"""
    if not text:
        return ''
    text = text.replace('\r', ';').replace('\n', ';')
    text = re.sub(r'^\s*cookie\s*:\s*', '', text.strip(), flags=re.I)
    return text.strip()


def parse_cookie_text(text: str) -> dict:
    """解析 Cookie 字符串为 {name: value}"""
    cookies = {}
    for part in normalize_cookie_text(text).split(';'):
        part = part.strip()
        if not part or '=' not in part:
            continue
        name, _, value = part.partition('=')
        name = name.strip()
        value = value.strip().strip('"')
        if not name or name.lower() in _COOKIE_ATTRS:
            continue
        cookies[name] = value
    return cookies


def _parse_set_cookie_header(raw):
    """解析单条 Set-Cookie 头

    不用 http.cookies.SimpleCookie：新浪下发的是
    "expires=Sunday, 31-Oct-2027 03:04:33 GMT" 这种非标准日期（完整星期名 +
    连字符日期），SimpleCookie 的正则匹配不上会「整条静默丢弃」，既不报错也拿
    不到值 —— 这就是游客 Cookie 到期时间一直是未知的原因。
    """
    parts = raw.split(';')
    name, _, value = parts[0].partition('=')
    name = name.strip()
    if not name:
        return None, None, {}
    attrs = {}
    for item in parts[1:]:
        key, _, val = item.partition('=')
        key = key.strip().lower()
        if key:
            attrs[key] = val.strip()
    return name, value.strip(), attrs


def split_set_cookie(raw_headers):
    """解析 Set-Cookie 列表，返回 ({name: value}, {name: 本地过期时间})"""
    cookies, expires = {}, {}
    for raw in raw_headers or []:
        name, value, attrs = _parse_set_cookie_header(raw)
        if not name:
            continue
        domain = (attrs.get('domain') or '').lower()
        if domain and domain not in _CN_COOKIE_DOMAINS:
            continue
        if not value or value == 'deleted' or len(value) > 1024:
            continue
        try:
            max_age = int(attrs['max-age']) if 'max-age' in attrs else None
        except ValueError:
            max_age = None
        if max_age is not None and max_age <= 0:
            continue
        cookies[name] = value
        local = _to_local_str(attrs.get('expires'))
        if local:
            expires[name] = local
        elif max_age:
            expires[name] = (datetime.now() + timedelta(seconds=max_age)).strftime('%Y-%m-%d %H:%M:%S')
    return cookies, expires


def render_cookie(cookies: dict) -> str:
    return '; '.join(f'{k}={v}' for k, v in cookies.items() if v)


async def request_guest_cookie():
    """申请 .weibo.cn 域的游客身份 Cookie（新浪 h5 访客流程，无需账号密码）

    返回 (cookies, expires)；失败返回 None。
    """
    session = await _get_session()
    target = 'https://m.weibo.cn/'
    rid = str(int(time.time() * 1000))
    payload = {
        'cb': 'visitor_gray_callback',
        'ver': _VISITOR_VER,
        'request_id': f'wbmon{rid}',
        'tid': '',
        'from': 'weibo',
        'webdriver': 'false',
        'rid': rid,
        'return_url': target,
    }
    try:
        async with session.post(
            f'{_VISITOR_HOME}/visitor/genvisitor2',
            data=payload,
            headers={'Referer': f'{_VISITOR_HOME}/visitor/visitor?entry={_VISITOR_ENTRY}&a=enter'},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            text = await resp.text()
    except Exception as e:
        sv.logger.error(f"申请游客身份失败(genvisitor2): {type(e).__name__}: {e}")
        return None

    sub = re.search(r'"sub":"([^"]+)"', text)
    subp = re.search(r'"subp":"([^"]+)"', text)
    if not sub or not subp:
        sv.logger.error(f"申请游客身份响应异常: {text[:200]}")
        return None

    url = (f'{_VISITOR_HOME}/visitor/visitor?a=crossdomain'
           f'&s={quote(sub.group(1), safe="")}&sp={quote(subp.group(1), safe="")}'
           f'&from=weibo&_rand={random.random()}&entry={_VISITOR_ENTRY}'
           f'&url={quote(target, safe="")}')
    try:
        async with session.get(url, allow_redirects=False,
                               headers={'Referer': target},
                               timeout=aiohttp.ClientTimeout(total=15)) as resp:
            raw_cookies = resp.headers.getall('Set-Cookie', [])
    except Exception as e:
        sv.logger.error(f"申请游客身份失败(crossdomain): {type(e).__name__}: {e}")
        return None

    cookies, expires = split_set_cookie(raw_cookies)
    # crossdomain 没直接下发时，退回 genvisitor2 返回的 sub/subp
    cookies.setdefault('SUB', sub.group(1))
    cookies.setdefault('SUBP', subp.group(1))
    return cookies, expires


class CookieStore:
    """m.weibo.cn 的 Cookie 仓库：手工 Cookie 优先，游客 Cookie 自动兜底"""

    def __init__(self, raw=None):
        raw = raw or {}
        self.manual_raw = str(raw.get('cookie') or '')
        self.manual_cookies = parse_cookie_text(self.manual_raw)
        self.manual_xsrf = str(raw.get('xsrf_token') or '')
        self.manual_invalid = bool(raw.get('manual_invalid'))
        self.manual_expire_at = str(raw.get('manual_expire_at') or '')
        self.guest = dict(raw.get('guest_cookies') or {})
        self.guest_xsrf = str(raw.get('guest_xsrf') or '')
        self.guest_fetched_at = str(raw.get('guest_fetched_at') or '')
        self.guest_expire_at = str(raw.get('guest_expire_at') or '')
        self._last_save = 0.0

    # ---------------- 持久化 ----------------
    def manual_header(self) -> str:
        if self.manual_cookies:
            return render_cookie(self.manual_cookies)
        return self.manual_raw

    def to_dict(self) -> dict:
        return {
            'cookie': self.manual_header(),
            'xsrf_token': self.manual_xsrf,
            'manual_invalid': self.manual_invalid,
            'manual_expire_at': self.manual_expire_at,
            'guest_cookies': self.guest,
            'guest_xsrf': self.guest_xsrf,
            'guest_fetched_at': self.guest_fetched_at,
            'guest_expire_at': self.guest_expire_at,
        }

    def save(self, force: bool = False) -> None:
        """写回 data.json；非强制时 10 秒内合并一次，避免频繁落盘"""
        now = time.time()
        if not force and now - self._last_save < 10:
            return
        self._last_save = now
        try:
            _atomic_write_json(DATA_FILE, self.to_dict())
        except Exception as e:
            sv.logger.error(f"保存 Cookie 失败: {e}")

    # ---------------- 状态查询 ----------------
    @property
    def has_manual(self) -> bool:
        return bool(self.manual_header()) and not self.manual_invalid

    def source(self) -> str:
        if self.has_manual:
            return '手工 Cookie'
        if self.guest:
            return '自动游客 Cookie'
        return '无'

    def cookie_header(self) -> str:
        if self.has_manual:
            return self.manual_header()
        return render_cookie(self.guest)

    def xsrf(self) -> str:
        if self.has_manual and self.manual_xsrf:
            return self.manual_xsrf
        return self.guest_xsrf

    def active_expire_at(self) -> str:
        return self.manual_expire_at if self.has_manual else self.guest_expire_at

    @staticmethod
    def _days_left_at(value: str):
        if not value:
            return None
        try:
            expire = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
        except ValueError:
            return None
        return (expire - datetime.now()).total_seconds() / 86400.0

    def days_left(self):
        return self._days_left_at(self.active_expire_at())

    def manual_days_left(self):
        return self._days_left_at(self.manual_expire_at)

    def guest_days_left(self):
        return self._days_left_at(self.guest_expire_at)

    # ---------------- 变更操作 ----------------
    def set_manual(self, text: str) -> bool:
        cookies = parse_cookie_text(text)
        if not cookies:
            return False
        self.manual_raw = normalize_cookie_text(text)
        self.manual_cookies = cookies
        self.manual_xsrf = cookies.get('XSRF-TOKEN', '')
        self.manual_invalid = False
        self.manual_expire_at = ''
        self.save(force=True)
        return True

    def clear_manual(self) -> None:
        self.manual_raw = ''
        self.manual_cookies = {}
        self.manual_xsrf = ''
        self.manual_invalid = False
        self.manual_expire_at = ''
        self.save(force=True)

    def absorb(self, raw_headers) -> None:
        """把响应里的 Set-Cookie 滚动合并进来（续期 / 保持身份新鲜）"""
        cookies, expires = split_set_cookie(raw_headers)
        if not cookies:
            return
        if self.has_manual:
            self.manual_cookies.update(cookies)
            if 'SUB' in expires:
                self.manual_expire_at = expires['SUB']
        else:
            self.guest.update(cookies)
            if cookies.get('XSRF-TOKEN'):
                self.guest_xsrf = cookies['XSRF-TOKEN']
            if 'SUB' in expires:
                self.guest_expire_at = expires['SUB']
        self.save()

    async def refresh_guest(self) -> bool:
        result = await request_guest_cookie()
        if not result:
            return False
        cookies, expires = result
        self.guest = cookies
        self.guest_xsrf = cookies.get('XSRF-TOKEN', '')
        self.guest_fetched_at = _now_str()
        self.guest_expire_at = expires.get('SUB') or expires.get('SUBP') or ''
        self.save(force=True)
        sv.logger.info(f"已更新自动游客 Cookie（过期时间: {self.guest_expire_at or '未知'}）")
        return True

    async def ensure_ready(self) -> bool:
        """确保手上有一套可用 Cookie；游客身份临近过期时提前续期"""
        if self.cookie_header():
            if not self.has_manual and self.guest:
                left = self.guest_days_left()
                if left is not None and left < 3:
                    sv.logger.info("游客 Cookie 临近过期，提前自动续期")
                    await self.refresh_guest()
            return True
        return await self.refresh_guest()

    async def handle_auth_failure(self) -> bool:
        """认证失败处理：手工 Cookie 降级到游客身份，并刷新游客身份

        返回 True 表示已经换过 Cookie，调用方可以重试一次。
        """
        changed = False
        if self.has_manual:
            self.manual_invalid = True
            changed = True
            self.save(force=True)
            sv.logger.warning("手工 Cookie 已失效，自动降级为游客身份")
        if await self.refresh_guest():
            changed = True
        return changed


COOKIES = CookieStore(_load_json_file(DATA_FILE, {}))


async def weibo_api_get(url, referer='', retry=2, stop_on_login=False, allow_fallback=True):
    """统一的 m.weibo.cn 请求入口

    返回 (body, state)，state 取值：
      'ok'             —— 正常拿到 JSON（ok 字段由调用方判断）
      'expired'        —— Cookie 失效，且自动续期后依旧失败
      'login_required' —— 该接口需要真实登录（游客身份只能读第 1 页）
      'error'          —— 其它网络/协议错误
    """
    session = await _get_session()
    await COOKIES.ensure_ready()

    refreshed = False   # 是否已经因为认证失败换过一套 Cookie（换 Cookie 不消耗重试次数）
    attempt = 0
    while attempt <= retry:
        headers = {
            'Referer': referer or f'{API_BASE}/',
            'X-Requested-With': 'XMLHttpRequest',
            'MWeibo-Pwa': '1',
        }
        cookie = COOKIES.cookie_header()
        if cookie:
            headers['Cookie'] = cookie
        xsrf = COOKIES.xsrf()
        if xsrf:
            headers['X-XSRF-TOKEN'] = xsrf

        consumed_attempt = True
        try:
            async with session.get(url, headers=headers, allow_redirects=False,
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:
                raw_cookies = resp.headers.getall('Set-Cookie', [])

                if resp.status in (301, 302, 303, 307, 308):
                    location = resp.headers.get('Location', '') or ''
                    if 'passport' in location or 'signin' in location or '/login' in location:
                        if stop_on_login:
                            return None, 'login_required'
                        sv.logger.warning(f"微博接口要求登录（HTTP {resp.status}）")
                        if allow_fallback and not refreshed:
                            refreshed = True
                            consumed_attempt = False
                            if await COOKIES.handle_auth_failure():
                                continue
                        return None, 'expired'
                    sv.logger.warning(f"微博接口异常跳转({resp.status}): {location[:120]}")
                    return None, 'error'

                if resp.status in (401, 403, 432):
                    sv.logger.warning(f"微博接口返回 {resp.status}（风控或 Cookie 失效）")
                    if allow_fallback and not refreshed:
                        refreshed = True
                        consumed_attempt = False
                        if await COOKIES.handle_auth_failure():
                            continue
                    return None, 'expired'

                if resp.status != 200:
                    sv.logger.warning(f"微博接口返回 HTTP {resp.status}")
                else:
                    content_type = (resp.headers.get('Content-Type') or '').lower()
                    if 'json' not in content_type:
                        preview = (await resp.text())[:160].replace('\n', ' ')
                        sv.logger.warning(f"微博接口非 JSON 响应: {content_type} {preview}")
                    else:
                        body = await resp.json(content_type=None)
                        COOKIES.absorb(raw_cookies)
                        if isinstance(body, dict) and body.get('ok') == -100:
                            # 游客身份翻第 2 页会拿到 ok=-100（等价于要求登录），
                            # 这不是 Cookie 坏了，别误判成失效去刷新/报警。
                            sv.logger.warning("微博接口返回 ok=-100（风控或需要登录）")
                            if stop_on_login:
                                return None, 'login_required'
                            if allow_fallback and not refreshed:
                                refreshed = True
                                consumed_attempt = False
                                if await COOKIES.handle_auth_failure():
                                    continue
                            return None, 'expired'
                        return body, 'ok'
        except asyncio.CancelledError:
            raise
        except Exception as e:
            sv.logger.warning(f"微博接口请求异常(第{attempt + 1}次): {type(e).__name__}: {e}")

        if consumed_attempt:
            attempt += 1
        if attempt <= retry:
            await asyncio.sleep(2 + attempt * 2)

    return None, 'error'


# 初始化配置
load_config()
  
# 说明：原 HTML 降级解析函数 parse_html_response 已删除。
# 原因：m.weibo.cn/u/<uid> 现在返回的是 Vue 骨架页（$render_data 为空，也没有
# div.c / id="M_" 的卡片），XPath 永远解析不到内容 —— 属于「必然失败、还白打
# 两次请求」的死代码。真正能拿到数据的只有 getIndex 接口。
        
async def get_weibo_user_info(uid, retry=2, force_refresh=False):
    """获取微博用户信息（带缓存；失败不写缓存，避免把默认名永久缓存下来）"""
    if not str(uid).isdigit():
        return None

    if force_refresh and uid in weibo_config['account_cache']:
        del weibo_config['account_cache'][uid]
        save_config()

    if uid in weibo_config['account_cache']:
        cached_info = weibo_config['account_cache'][uid]
        if cached_info.get('uid') == uid:
            if not cached_info.get('name'):
                cached_info['name'] = f'用户{uid}'
                weibo_config['account_cache'][uid] = cached_info
                save_config()
            return cached_info
        sv.logger.warning(f"缓存UID不匹配，清除缓存: 缓存={cached_info.get('uid')}, 请求={uid}")
        del weibo_config['account_cache'][uid]
        save_config()

    url = f'{API_BASE}/api/container/getIndex?type=uid&value={uid}'
    body, state = await weibo_api_get(url, referer=f'{API_BASE}/u/{uid}', retry=retry)

    if state == 'ok' and isinstance(body, dict) and body.get('ok') == 1:
        user_info = (body.get('data') or {}).get('userInfo') or {}
        name = str(user_info.get('screen_name') or '').strip() or f'用户{uid}'
        result = {'name': name, 'uid': uid}
        weibo_config['account_cache'][uid] = result
        save_config()
        sv.logger.info(f"成功获取用户{uid}信息: {name}")
        return result

    if state == 'ok':
        sv.logger.warning(f"用户{uid}信息为空，API返回: {body}")
        # 注意：不写缓存。旧版会把「用户{uid}」缓存下来，一次网络抖动就永久显示错名
        return {'name': f'用户{uid}', 'uid': uid}

    sv.logger.error(f"用户{uid}信息获取失败(state={state})")
    return None

    
async def get_weibo_user_latest_posts(uid, count=5, retry=2):
    """获取用户最新微博（m.weibo.cn API）

    游客身份只能读第 1 页（约 10 条），翻第 2 页会被要求登录 —— 这时直接停下
    返回已取到的内容，不重试、不报警；配置了可用的手工 Cookie 才能继续翻页。
    """
    all_posts = []
    page = 1
    max_pages = max(1, min(5, math.ceil(count / 10) + 1))

    while len(all_posts) < count and page <= max_pages:
        url = (f'{API_BASE}/api/container/getIndex?type=uid&value={uid}'
               f'&containerid=107603{uid}&page={page}')
        body, state = await weibo_api_get(
            url, referer=f'{API_BASE}/u/{uid}', retry=retry, stop_on_login=(page > 1))

        if state == 'login_required':
            sv.logger.info(f"微博{uid}第{page}页需要登录，游客身份只读第 1 页，停止翻页")
            break
        if state == 'expired':
            raise CookieExpiredError('接口拒绝访问，自动续期后仍失败')
        if state != 'ok':
            break

        if not isinstance(body, dict) or body.get('ok') != 1:
            msg = str((body or {}).get('msg') or '')
            # ok=0 + "这里还没有内容" 是「这个账号没发过微博」，不是错误
            if '没有内容' in msg or (isinstance(body, dict) and body.get('ok') == 0):
                sv.logger.info(f"微博{uid}暂无微博内容（{msg or 'ok=0'}）")
            else:
                sv.logger.warning(f"微博{uid}API返回失败: {msg or body}")
            break

        cards = (body.get('data') or {}).get('cards') or []
        for card in cards:
            if card.get('card_type') != 9:
                continue
            mblog = card.get('mblog') or {}
            if not mblog:
                continue

            raw_text = mblog.get('text', '') or ''
            text = re.sub(r'<br\s*/?>', '\n', raw_text)
            text = re.sub(r'<[^>]+>', '', text)
            text = html.unescape(text).strip() or '【无正文内容】'

            pic_urls = []
            for pic in mblog.get('pics') or []:
                large_url = (pic.get('large') or {}).get('url', '')
                if large_url:
                    pic_urls.append(large_url)
                elif pic.get('url'):
                    pic_urls.append(pic['url'])

            video_info = {'play_page_url': '', 'cover_url': ''}
            page_info = mblog.get('page_info') or {}
            if page_info and page_info.get('type') == 'video':
                media_info = page_info.get('media_info') or {}
                video_info['play_page_url'] = (media_info.get('stream_url_hd', '')
                                               or media_info.get('stream_url', ''))
                page_pic = page_info.get('page_pic')
                video_info['cover_url'] = (page_pic or {}).get('url', '') if isinstance(page_pic, dict) else str(page_pic or '')

            created_at = mblog.get('created_at', 'unknown')
            all_posts.append({
                'id': str(mblog.get('id', 'unknown')),
                'text': text,
                'pics': pic_urls,
                'video': video_info,
                'created_at': created_at,
                'created_time': format_weibo_time(created_at),
                'reposts_count': mblog.get('reposts_count', 0),
                'comments_count': mblog.get('comments_count', 0),
                'attitudes_count': mblog.get('attitudes_count', 0),
            })

            if len(all_posts) >= count:
                return all_posts[:count]

        page += 1
        if len(all_posts) < count and page <= max_pages:
            await asyncio.sleep(random.uniform(1.5, 3))

    return all_posts[:count]



_check_lock = None


def _get_check_lock():
    global _check_lock
    if _check_lock is None:
        _check_lock = asyncio.Lock()
    return _check_lock


async def notify_superusers(text: str) -> None:
    """私聊所有主人；失败只记日志，不抛异常"""
    try:
        bot = hoshino.get_bot()
        for superuser_id in hoshino.config.SUPERUSERS:
            try:
                await bot.send_private_msg(user_id=int(superuser_id), message=text)
            except Exception as e:
                sv.logger.error(f"通知主人 {superuser_id} 失败: {e}")
    except Exception as e:
        sv.logger.error(f"通知主人失败: {e}")


async def check_and_push_new_weibo():
    """检查新微博并推送（加互斥锁，避免定时任务和手动检查重复推送）"""
    lock = _get_check_lock()
    if lock.locked():
        sv.logger.info("上一次微博检查还没结束，跳过本次触发")
        return False
    async with lock:
        await _check_and_push_new_weibo()
    return True


async def _check_and_push_new_weibo():
    global _cookie_expired_notified
    sv.logger.info("开始检查微博更新...")

    all_followed_uids = set()
    for follows in weibo_config['group_follows'].values():
        all_followed_uids.update(follows.keys())
    if not all_followed_uids:
        sv.logger.info("没有需要检查的微博账号")
        return

    had_error = False
    for uid in sorted(all_followed_uids):
        try:
            # 取满一页（10 条），避免截断导致「新微博被静默丢掉」
            latest_posts = await get_weibo_user_latest_posts(uid, count=10)
            if not latest_posts:
                continue

            # 各群水位线取最早的一个，保证落后的群也能补上
            min_last_post_time = ''
            for follows in weibo_config['group_follows'].values():
                if uid in follows:
                    current_time = follows[uid].get('last_post_time', '')
                    if not min_last_post_time or current_time < min_last_post_time:
                        min_last_post_time = current_time

            new_posts = [p for p in latest_posts
                         if p.get('created_time') and p['created_time'] > min_last_post_time]
            if not new_posts:
                continue

            new_posts.sort(key=lambda x: x['created_time'])
            pushed_time = ''
            all_groups_to_update = set()

            for post in new_posts:
                groups_to_push = []
                for group_id, follows in weibo_config['group_follows'].items():
                    if (uid in follows
                            and weibo_config['group_enable'].get(group_id, True)
                            and post['created_time'] > follows[uid].get('last_post_time', '')):
                        groups_to_push.append(group_id)
                        all_groups_to_update.add(group_id)

                if groups_to_push:
                    user_info = await get_weibo_user_info(uid)
                    user_name = user_info['name'] if user_info else f'用户{uid}'
                    if user_info and user_info.get('uid') != uid:
                        user_info = await get_weibo_user_info(uid, force_refresh=True)
                        user_name = user_info['name'] if user_info else f'用户{uid}'
                    await push_weibo_to_groups(groups_to_push, user_name, uid, post)
                    pushed_time = max(pushed_time, post['created_time'])

            if all_groups_to_update and pushed_time:
                # 水位线用「真正推送过的最新微博时间」，而不是 now()：
                # 用 now() 会把「抓取期间刚发布的微博」永久跳过
                for group_id in all_groups_to_update:
                    weibo_config['group_follows'][group_id][uid]['last_post_time'] = pushed_time
                save_config()

        except CookieExpiredError as e:
            sv.logger.error(f"微博{uid}认证失败: {e}")
            if await COOKIES.handle_auth_failure():
                continue  # 已换成新 Cookie，后面的账号继续跑
            had_error = True
            if not _cookie_expired_notified:
                _cookie_expired_notified = True
                await notify_superusers(
                    "[微博推送] 微博接口认证失败，自动申请游客身份也没能成功，推送已暂停。\n"
                    "可用「微博cookie状态」查看详情，或「更新cookie [cookie字符串]」更新认证信息。"
                )
            break
        except Exception as e:
            sv.logger.error(f"处理微博{uid}时出错: {type(e).__name__}: {e}")
            had_error = True
            continue

    if not had_error:
        _cookie_expired_notified = False
    sv.logger.info("微博更新检查完成")


async def merge_images_to_grid(pic_urls: list) -> str:
    """将多张图片合并为九宫格，返回 CQ:image base64 字符串，失败返回 None"""
    try:
        pics = [u for u in (pic_urls or []) if u][:9]
        if len(pics) <= 2:
            return None

        img_headers = {
            'User-Agent': _MOBILE_UA,
            'Referer': 'https://m.weibo.cn/',
            'Accept': 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8',
        }

        session = await _get_session()
        images = []
        for url in pics:
            try:
                # 注意：不再用 ssl=False，图片站证书正常，关掉校验有害无益
                async with session.get(url, headers=img_headers,
                                       timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        sv.logger.warning(f"下载图片失败 HTTP {resp.status}: {url}")
                        continue
                    img_data = await resp.read()
                images.append(Image.open(BytesIO(img_data)).convert('RGB'))
            except Exception as e:
                sv.logger.warning(f"下载图片异常: {url}, {type(e).__name__}: {e}")

        if len(images) <= 2:
            sv.logger.warning(f"下载成功的图片不足3张({len(images)}张)，放弃合并")
            return None

        n = len(images)
        cols = 3 if n > 2 else n
        rows = math.ceil(n / cols)
        cell_size = 300
        gap = 4
        canvas_w = cols * cell_size + (cols - 1) * gap
        canvas_h = rows * cell_size + (rows - 1) * gap
        canvas = Image.new('RGB', (canvas_w, canvas_h), (255, 255, 255))

        for i, img in enumerate(images):
            fitted = ImageOps.fit(img, (cell_size, cell_size), method=Image.LANCZOS)
            canvas.paste(fitted, ((i % cols) * (cell_size + gap), (i // cols) * (cell_size + gap)))

        buf = BytesIO()
        canvas.save(buf, format='JPEG', quality=85)
        b64 = base64.b64encode(buf.getvalue()).decode()
        sv.logger.info(f"九宫格合并成功（{n} 图，base64 长度 {len(b64)}）")
        return f"[CQ:image,file=base64://{b64}]"
    except Exception as e:
        sv.logger.error(f"合并九宫格图片失败: {type(e).__name__}: {e}")
        return None

        
async def push_weibo_to_groups(group_ids, name, uid, post):  
    """推送微博到指定群（优先使用配置文件中的自定义名称）"""  
    # 优先从 group_follows 中获取自定义名称  
    custom_name = None  
    for group_id in group_ids:  
        if (group_id in weibo_config['group_follows'] and   
            uid in weibo_config['group_follows'][group_id]):  
            custom_name = weibo_config['group_follows'][group_id][uid].get('name')  
            if custom_name and custom_name.strip() and not custom_name.startswith('用户'):  
                break  
      
    if custom_name and custom_name.strip() and not custom_name.startswith('用户'):  
        # 使用配置文件中的自定义名称  
        name = custom_name  
        sv.logger.info(f"使用自定义名称: {name} (UID: {uid})")  
    else:  
        # 只有在没有自定义名称时才获取用户信息  
        user_info = await get_weibo_user_info(uid)  
        if user_info:  
            name = user_info['name']  
            sv.logger.info(f"使用API获取的名称: {name} (UID: {uid})")  
        else:  
            name = f'用户{uid}'  
      
    # 组装消息  
    msg_parts = [  
        f"📢 {name} (ID: {uid}) 发布新微博:",  
        f"{post['text']}\n\n"  
    ]   
      
    # 追加图片（多图时合并为九宫格）  
    if len(post['pics']) > 2:  
        grid_img = await merge_images_to_grid(post['pics'])  
        if grid_img:  
            msg_parts.append(f"{grid_img}\n")  
        else:  
            for pic_url in post['pics']:  
                if pic_url:  
                    msg_parts.append(f"[CQ:image,url={escape(pic_url)}]\n")  
    else:  
        for pic_url in post['pics']:  
            if pic_url:  
                msg_parts.append(f"[CQ:image,url={escape(pic_url)}]\n")
      
    # 追加统计和链接  
    msg_parts.extend([  
        f"\n👍 {post['attitudes_count']}  🔁 {post['reposts_count']}  💬 {post['comments_count']}",  
        f"\n发布时间：{post['created_time']}",
        f"\n原文链接：https://m.weibo.cn/status/{post['id']}",  
        f"\n取消关注请使用：取消关注微博 {uid}"  
    ])  
      
    full_msg = ''.join(msg_parts)  
      
    # 发送到每个群（避免发送过快）  
    for group_id in group_ids:  
        try:  
            await sv.bot.send_group_msg(group_id=int(group_id), message=full_msg)  
            await asyncio.sleep(3)  
        except Exception as e:  
            sv.logger.error(f"向群{group_id}推送失败: {e}，消息预览: {full_msg[:200]}...")


# -------------------------- 定时任务（调整为20分钟减少反爬） --------------------------
@sv.scheduled_job('cron', minute='*/20')  # 每20分钟执行一次
async def scheduled_check_weibo():
    # 新增：随机延迟0-5分钟，避免整点高频请求
    await asyncio.sleep(random.uniform(0, 300))
    await check_and_push_new_weibo()

# 关注微博账号
@sv.on_prefix(('关注微博', '订阅微博'))  
async def follow_weibo(bot, ev: CQEvent):  
    group_id = str(ev.group_id)  
    user_id = ev.user_id  
      
    if not _nlmt.check(user_id):  
        await bot.finish(ev, '今日关注微博次数已达上限，请明天再试~')  
    if not flmt.check(user_id):  
        await bot.finish(ev, f'操作太频繁啦，请{int(flmt.left_time(user_id)) + 1}秒后再试~')  
      
    # 解析UID和微博名  
    text = ev.message.extract_plain_text().strip()  
    parts = text.split(maxsplit=1)  
    uid = parts[0] if parts else ''  
    name = parts[1] if len(parts) > 1 else f'用户{uid}'  
      
    if not uid:  
        await bot.finish(ev, '请输入要关注的微博ID和微博名，格式：关注微博 UID 微博名')  
      
    # 检查是否在本群黑名单中  
    group_blacklist = weibo_config['group_blacklist'].get(group_id, set())  
    if uid in group_blacklist:  
        await bot.finish(ev, f'该微博ID({uid})已在本群黑名单中，禁止关注~')  
      
    # 验证微博ID有效性（仅验证，不获取名称）  
    user_info = await get_weibo_user_info(uid, force_refresh=True)  
    if not user_info:  
        await bot.finish(ev, f'未查询到微博ID为{uid}的用户，请检查ID是否正确~')  
      
    if group_id not in weibo_config['group_follows']:  
        weibo_config['group_follows'][group_id] = {}  
      
    if uid in weibo_config['group_follows'][group_id]:  
        saved_name = weibo_config['group_follows'][group_id][uid]['name']  
        await bot.finish(ev, f'本群已经关注过 {saved_name} 啦~')  
      
    # 直接使用当前时间  
    current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')  
      
    # 保存到配置（使用命令中提供的名称）  
    weibo_config['group_follows'][group_id][uid] = {  
        'name': name,  
        'last_post_time': current_time  
    }  
      
    if group_id not in weibo_config['group_enable']:  
        weibo_config['group_enable'][group_id] = True  
      
    save_config()  
    _nlmt.increase(user_id)  
    flmt.start_cd(user_id)  
    await bot.send(ev, f'本群成功关注 {name} 的微博啦~ 有新动态会第一时间通知哦~')

@sv.on_prefix(('全群关注微博', '全群订阅微博'))  
async def follow_weibo_all_groups(bot, ev: CQEvent):  
    user_id = ev.user_id  
      
    # 仅允许管理员执行全群操作  
    if not priv.check_priv(ev, priv.ADMIN):  
        await bot.finish(ev, '只有管理员才能操作全群关注哦~')  
      
    if not _nlmt.check(user_id):  
        await bot.finish(ev, '今日全群关注微博次数已达上限，请明天再试~')  
    if not flmt.check(user_id):  
        await bot.finish(ev, f'操作太频繁啦，请{int(flmt.left_time(user_id)) + 1}秒后再试~')  
      
    # 解析UID和微博名  
    text = ev.message.extract_plain_text().strip()  
    parts = text.split(maxsplit=1)  
    uid = parts[0] if parts else ''  
    name = parts[1] if len(parts) > 1 else f'用户{uid}'  
      
    if not uid:  
        await bot.finish(ev, '请输入要全群关注的微博ID和微博名，格式：全群关注微博 UID 微博名')  
      
    # 验证微博ID有效性（仅验证，不获取名称）  
    user_info = await get_weibo_user_info(uid, force_refresh=True)  
    if not user_info:  
        await bot.finish(ev, f'未查询到微博ID为{uid}的用户，请检查ID是否正确~')  
      
    # 获取所有已加入的群  
    groups = await bot.get_group_list()  
    if not groups:  
        await bot.finish(ev, '未加入任何群组，无法进行全群关注~')  
      
    # 直接使用当前时间  
    current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')  
      
    # 记录受影响的群数量  
    new_follow_count = 0  
      
    for group in groups:  
        group_id = str(group['group_id'])  
          
        # 检查该群是否将该uid加入黑名单，若是则跳过  
        group_blacklist = weibo_config['group_blacklist'].get(group_id, set())  
        if uid in group_blacklist:  
            continue  # 跳过该群  
          
        # 初始化群配置（如果不存在）  
        if group_id not in weibo_config['group_follows']:  
            weibo_config['group_follows'][group_id] = {}  
          
        # 仅处理未关注的群  
        if uid not in weibo_config['group_follows'][group_id]:  
            weibo_config['group_follows'][group_id][uid] = {  
                'name': name,  
                'last_post_time': current_time  
            }  
            new_follow_count += 1  
          
        # 确保开启推送  
        weibo_config['group_enable'][group_id] = True  
      
    save_config()  
    _nlmt.increase(user_id)  
    flmt.start_cd(user_id)  
    await bot.send(ev, f'成功为{new_follow_count}个群开启 {name} 的微博关注~ 有新动态会第一时间通知哦~')

# 群内黑名单管理命令
@sv.on_prefix(('微博黑名单', '添加微博黑名单'))
async def add_blacklist(bot, ev: CQEvent):
    # 仅允许管理员操作
    if not priv.check_priv(ev, priv.ADMIN):
        await bot.finish(ev, '只有管理员才能操作黑名单哦~')
    
    group_id = str(ev.group_id)
    uid = ev.message.extract_plain_text().strip()
    if not uid:
        await bot.finish(ev, '请输入要加入黑名单的微博ID哦~')
    
    # 初始化该群的黑名单（如果不存在）
    if group_id not in weibo_config['group_blacklist']:
        weibo_config['group_blacklist'][group_id] = set()
    
    if uid in weibo_config['group_blacklist'][group_id]:
        await bot.finish(ev, f'该微博ID({uid})已在本群黑名单中~')
    
    # 加入黑名单
    weibo_config['group_blacklist'][group_id].add(uid)
    
    # 自动取消该群对该ID的关注
    if group_id in weibo_config['group_follows'] and uid in weibo_config['group_follows'][group_id]:
        del weibo_config['group_follows'][group_id][uid]
        save_config()  # 先保存取消关注的修改
        await bot.send(ev, f'已自动取消本群对微博ID({uid})的关注~')
    
    save_config()
    await bot.send(ev, f'已成功将微博ID({uid})加入本群黑名单，禁止关注~')

@sv.on_prefix(('微博黑名单移除', '移除微博黑名单'))
async def remove_blacklist(bot, ev: CQEvent):
    # 仅允许管理员操作
    if not priv.check_priv(ev, priv.ADMIN):
        await bot.finish(ev, '只有管理员才能操作黑名单哦~')
    
    group_id = str(ev.group_id)
    uid = ev.message.extract_plain_text().strip()
    if not uid:
        await bot.finish(ev, '请输入要移除黑名单的微博ID哦~')
    
    # 检查该群黑名单是否存在
    if group_id not in weibo_config['group_blacklist'] or uid not in weibo_config['group_blacklist'][group_id]:
        await bot.finish(ev, f'该微博ID({uid})不在本群黑名单中~')
    
    # 移除黑名单
    weibo_config['group_blacklist'][group_id].remove(uid)
    save_config()
    await bot.send(ev, f'已成功将微博ID({uid})从本群黑名单中移除~')

# 取消关注微博账号
@sv.on_prefix(('取消关注微博', '取消订阅微博'))
async def unfollow_weibo(bot, ev: CQEvent):
    group_id = str(ev.group_id)
    uid = ev.message.extract_plain_text().strip()
    if not uid:
        await bot.finish(ev, '请输入要取消关注的微博ID哦~')
    
    if group_id not in weibo_config['group_follows'] or uid not in weibo_config['group_follows'][group_id]:
        await bot.finish(ev, '本群没有关注这个微博账号哦~')
    
    name = weibo_config['group_follows'][group_id][uid]['name']
    del weibo_config['group_follows'][group_id][uid]
    save_config()
    await bot.send(ev, f'本群已取消关注 {name} 的微博~')

@sv.on_prefix(('全群取消关注微博', '全群取消订阅微博'))  
async def unfollow_weibo_all_groups(bot, ev: CQEvent):  
    user_id = ev.user_id  
      
    # 仅允许管理员执行全群操作  
    if not priv.check_priv(ev, priv.ADMIN):  
        await bot.finish(ev, '只有管理员才能操作全群取消关注哦~')  
      
    if not _nlmt.check(user_id):  
        await bot.finish(ev, '今日全群取消关注微博次数已达上限,请明天再试~')  
    if not flmt.check(user_id):  
        await bot.finish(ev, f'操作太频繁啦,请{int(flmt.left_time(user_id)) + 1}秒后再试~')  
      
    uid = ev.message.extract_plain_text().strip()  
    if not uid:  
        await bot.finish(ev, '请输入要全群取消关注的微博ID哦~')  
      
    # 获取用户信息(用于显示名称)  
    user_info = await get_weibo_user_info(uid, force_refresh=True)  
    user_name = user_info['name'] if user_info else f'用户{uid}'  
      
    # 记录取消关注的群数量  
    unfollow_count = 0  
      
    # 遍历所有群的关注列表  
    for group_id in list(weibo_config['group_follows'].keys()):  
        if uid in weibo_config['group_follows'][group_id]:  
            del weibo_config['group_follows'][group_id][uid]  
            unfollow_count += 1  
      
    save_config()  
    _nlmt.increase(user_id)  
    flmt.start_cd(user_id)  
    await bot.send(ev, f'成功为{unfollow_count}个群取消关注 {user_name} 的微博~')

# 查看已关注的微博账号
@sv.on_fullmatch(('查看关注的微博', '查看订阅的微博'))
async def list_followed_weibo(bot, ev: CQEvent):
    group_id = str(ev.group_id)
    follows = weibo_config['group_follows'].get(group_id, {})
    if not follows:
        await bot.finish(ev, '本群还没有关注任何微博账号哦~')
    
    msg = "本群关注的微博账号：\n"
    for uid, info in follows.items():
        msg += f"- {info['name']} (ID: {uid})\n"
    msg += "\n取消关注请使用：取消关注微博 [ID]"
    await bot.send(ev, msg)

# 本群微博推送开关
@sv.on_prefix(('微博推送开关', '微博订阅开关'))
async def toggle_weibo_push(bot, ev: CQEvent):
    group_id = str(ev.group_id)
    
    if not priv.check_priv(ev, priv.ADMIN):
        await bot.finish(ev, '只有管理员才能操作哦~')
    
    status = ev.message.extract_plain_text().strip().lower()
    if status == 'on':
        weibo_config['group_enable'][group_id] = True
        save_config()
        await bot.send(ev, '本群微博推送已开启~')
    elif status == 'off':
        weibo_config['group_enable'][group_id] = False
        save_config()
        await bot.send(ev, '本群微博推送已关闭~')
    else:
        await bot.send(ev, '请输入"微博推送开关 on"开启或"微博推送开关 off"关闭~')

# 帮助信息
@sv.on_fullmatch(('微博推送帮助', '微博订阅帮助'))  
async def weibo_help(bot, ev: CQEvent):  
    help_msg = '''微博推送插件帮助:  
- 关注微博 [微博ID+微博名]:关注指定微博账号(仅本群生效)  
- 全群关注微博 [微博ID+微博名]:所有已加入的群都关注并开启推送(管理员)  
- 取消关注微博 [微博ID]:取消关注指定微博账号(仅本群生效)  
- 全群取消关注微博 [微博ID]:所有已加入的群都取消关注(管理员)  
- 查看关注的微博:查看本群已关注的微博账号  
- 微博推送开关 [on/off]:开启或关闭本群微博推送(管理员)  
- 微博黑名单 [ID]:将指定微博ID加入本群黑名单(管理员)  
- 微博黑名单移除 [ID]:将指定微博ID从本群黑名单移除(管理员)  
- 查看微博黑名单:查看本群黑名单中的微博ID(管理员)  
- 官方半月刊：查看PCR半月刊
- 微博cookie状态:查看 Cookie 状态与实时校验(管理员)
- 更新cookie [cookie]:更新手工 Cookie(超管，可选)
- 清除cookie:丢弃手工 Cookie，改回自动游客身份(超管)
- 检查微博更新
注:微博ID是指微博的数字ID,不是昵称哦~'''  
    await bot.send(ev, help_msg)

# 查看本群微博黑名单
@sv.on_fullmatch(('查看微博黑名单',))
async def check_blacklist(bot, ev: CQEvent):
    if not priv.check_priv(ev, priv.ADMIN):
        await bot.finish(ev, '只有管理员才能查看黑名单哦~')
    
    group_id = str(ev.group_id)
    blacklist = weibo_config['group_blacklist'].get(group_id, set())
    
    if not blacklist:
        await bot.send(ev, '本群黑名单为空~')
        return
    
    msg = "本群微博黑名单中的ID：\n"
    for uid in blacklist:
        msg += f"- {uid}\n"
    await bot.send(ev, msg)

@sv.on_prefix(('查看微博',))    
async def view_weibo(bot, ev: CQEvent):  
    user_id = ev.user_id    
      
    # 频率限制  
    if not _nlmt.check(user_id):  
        await bot.finish(ev, '今日查看微博次数已达上限,请明天再试~')  
    if not flmt.check(user_id):  
        await bot.finish(ev, f'操作太频繁啦,请{int(flmt.left_time(user_id)) + 1}秒后再试~')  
      
    uid = ev.message.extract_plain_text().strip()  
    if not uid:  
        await bot.finish(ev, '请输入要查看的微博ID哦~')  
      
    # 获取用户信息  
    user_info = await get_weibo_user_info(uid, force_refresh=True)  
    if not user_info:  
        await bot.finish(ev, f'未查询到微博ID为{uid}的用户,请检查ID是否正确~')  
      
    # 获取最新5条微博  
    try:  
        posts = await get_weibo_user_latest_posts(uid, count=5)  
    except CookieExpiredError as e:
        await notify_superusers(
            f"[微博推送] 微博接口认证失败（{e}），自动申请游客身份也没能成功。\n"
            "可用「微博cookie状态」查看详情，或「更新cookie [cookie字符串]」更新认证信息。"
        )
        await bot.finish(ev, '微博接口认证异常，已通知主人，请稍后再试~')
        return
    if not posts:  
        await bot.finish(ev, f'{user_info["name"]} 暂无微博内容~')  
      
    # 组装消息  
    msg_parts = [f'📱 {user_info["name"]} (ID: {uid}) 的最新{len(posts)}条微博:\n\n']  
      
    for i, post in enumerate(posts, 1):  
        msg_parts.append(f'【{i}】{post["text"][:100]}...\n' if len(post["text"]) > 100 else f'【{i}】{post["text"]}\n')  
          
        # 添加图片（多图时合并为九宫格）  
        if len(post['pics']) > 2:  
            grid_img = await merge_images_to_grid(post['pics'])  
            if grid_img:  
                msg_parts.append(grid_img)  
            else:  
                for pic_url in post['pics'][:3]:  
                    if pic_url:  
                        msg_parts.append(f'[CQ:image,url={escape(pic_url)}]')  
        else:  
            for pic_url in post['pics']:  
                if pic_url:  
                    msg_parts.append(f'[CQ:image,url={escape(pic_url)}]')
          
        msg_parts.append(f'\n👍 {post["attitudes_count"]}  🔁 {post["reposts_count"]}  💬 {post["comments_count"]}')  
        msg_parts.append(f'\n发布时间: {post["created_time"]}')
        msg_parts.append(f'\n链接: https://m.weibo.cn/status/{post["id"]}\n\n')  
      
    _nlmt.increase(user_id)  
    flmt.start_cd(user_id)  
    await bot.send(ev, ''.join(msg_parts))

@sv.on_fullmatch(('官方半月刊', '查看官方半月刊'))  
async def get_official_biweekly(bot, ev: CQEvent):  
    try:  
        user_id = ev.user_id  
          
        # 频率限制检查  
        if not _nlmt.check(user_id):  
            await bot.finish(ev, '今日查询次数已达上限，请明天再试~')  
        if not flmt.check(user_id):  
            await bot.finish(ev, f'操作太频繁啦，请{int(flmt.left_time(user_id)) + 1}秒后再试~')  
          
        uid = '6603867494'  # 官方账号ID  
          
        # 获取用户信息  
        user_info = await get_weibo_user_info(uid)  
        if not user_info:  
            await bot.finish(ev, '❌ 获取官方账号信息失败\n'  
                              '💡 建议管理员执行"更新cookie"命令更新认证信息')  
            return  
          
        # 获取最新70条微博(增加数量以提高找到半月刊的概率)  
        try:  
            posts = await get_weibo_user_latest_posts(uid, count=70)  
        except CookieExpiredError as e:
            await notify_superusers(
                f"[微博推送] 微博接口认证失败（{e}），自动申请游客身份也没能成功。\n"
                "可用「微博cookie状态」查看详情，或「更新cookie [cookie字符串]」更新认证信息。"
            )
            await bot.finish(ev, '❌ 微博接口认证异常，已通知主人，请稍后再试')
            return
          
        # 查找包含"活动半月刊"的微博  
        biweekly_post = None  
        for post in posts:  
            if '活动半月刊' in post['text']:  
                biweekly_post = post  
                break  
          
        if not biweekly_post:  
            await bot.finish(ev, '❌ 未找到最新的活动半月刊微博\n'  
                              '💡 请稍后重试或联系管理员检查账号状态')  
            return  
          
        # 组装消息  
        msg_parts = [  
            f"📢 {user_info['name']} 最新活动半月刊：\n\n",  
            f"{biweekly_post['text']}\n\n"  
        ]  
          
        # 添加图片（多图时合并为九宫格）  
        if len(biweekly_post['pics']) > 2:  
            grid_img = await merge_images_to_grid(biweekly_post['pics'])  
            if grid_img:  
                msg_parts.append(f"{grid_img}\n")  
            else:  
                for pic_url in biweekly_post['pics']:  
                    if pic_url:  
                        msg_parts.append(f"[CQ:image,url={escape(pic_url)}]\n")  
        else:  
            for pic_url in biweekly_post['pics']:  
                if pic_url:  
                    msg_parts.append(f"[CQ:image,url={escape(pic_url)}]\n")
          
        # 添加统计和链接  
        msg_parts.extend([  
            f"\n👍 {biweekly_post['attitudes_count']}  🔁 {biweekly_post['reposts_count']}  💬 {biweekly_post['comments_count']}",  
            f"\n发布时间：{biweekly_post['created_time']}",
            f"\n原文链接：https://m.weibo.cn/status/{biweekly_post['id']}"  
        ])  
          
        _nlmt.increase(user_id)  
        flmt.start_cd(user_id)  
        await bot.send(ev, ''.join(msg_parts))  
          
    except Exception as e:  
        sv.logger.error(f"获取官方半月刊失败: {e}")  
        await bot.finish(ev, f'❌ 获取半月刊时发生错误: {str(e)}\n'  
                          '💡 请稍后重试或联系管理员处理')

# -------------------------- Cookie 管理命令 --------------------------
_PROBE_UID_FALLBACK = '6603867494'  # 公主连结官方号，仅用于健康检查


def _pick_probe_uid(group_id=None):
    if group_id:
        follows = weibo_config['group_follows'].get(str(group_id)) or {}
        if follows:
            return next(iter(follows))
    for follows in weibo_config['group_follows'].values():
        if follows:
            return next(iter(follows))
    return _PROBE_UID_FALLBACK


async def _validate_cookie(probe_uid=None):
    """真正打一次时间线接口来验证 Cookie；返回 (是否可用, 说明文本)"""
    probe_uid = probe_uid or _pick_probe_uid()
    body, state = await weibo_api_get(
        f'{API_BASE}/api/container/getIndex?type=uid&value={probe_uid}&containerid=107603{probe_uid}',
        referer=f'{API_BASE}/u/{probe_uid}', retry=0, allow_fallback=False)
    if state == 'ok' and isinstance(body, dict) and body.get('ok') == 1:
        cards = [c for c in ((body.get('data') or {}).get('cards') or [])
                 if c.get('card_type') == 9]
        return True, f'读取 {probe_uid} 时间线成功（{len(cards)} 条）'
    if state == 'ok':
        return False, f'接口返回 ok={(body or {}).get("ok")} msg={(body or {}).get("msg")}'
    return False, f'请求失败（{state}）'


@sv.on_prefix('更新cookie')
async def update_weibo_cookie(bot, ev: CQEvent):
    """更新手工微博 Cookie（仅超级管理员可用，可选功能）"""
    if not priv.check_priv(ev, priv.SUPERUSER):
        await bot.finish(ev, '仅超级管理员可更新Cookie！')

    cookie_text = ev.message.extract_plain_text().strip()
    if not cookie_text:
        await bot.finish(ev, '请输入完整的Cookie内容，格式：更新cookie [cookie字符串]')

    cookies = parse_cookie_text(cookie_text)
    if not cookies:
        await bot.finish(ev, '没能从内容里解析出 Cookie，请确认粘贴的是形如「SUB=xxx; SUBP=xxx」的内容~')
    if 'SUB' not in cookies:
        await bot.finish(ev, 'Cookie 里缺少 SUB 字段，请从 m.weibo.cn 重新复制完整 Cookie~')

    if not COOKIES.set_manual(cookie_text):
        await bot.finish(ev, 'Cookie 保存失败，请检查内容~')

    ok, detail = await _validate_cookie(_pick_probe_uid(ev.group_id))
    if ok:
        await bot.send(ev, f'Cookie更新成功！{detail}。\n'
                           '（失效时会自动降级为游客身份，可用「微博cookie状态」随时查看）')
    else:
        await bot.send(ev, f'Cookie 已保存，但实时校验没通过：{detail}\n'
                           '机器人会继续用它尝试；确认失效后会自动改用游客身份，不会停摆。')


@sv.on_fullmatch(('微博cookie状态', '微博Cookie状态', 'cookie状态'))
async def weibo_cookie_status(bot, ev: CQEvent):
    """查看 Cookie 状态与实时校验（管理员）"""
    if not priv.check_priv(ev, priv.ADMIN):
        await bot.finish(ev, '只有管理员才能查看哦~')

    lines = ['📋 微博接口 Cookie 状态', f'当前使用：{COOKIES.source()}']

    if COOKIES.manual_raw:
        if COOKIES.manual_invalid:
            lines.append('手工 Cookie：已失效（已自动降级为游客身份）')
        else:
            lines.append('手工 Cookie：使用中')
        if COOKIES.manual_expire_at:
            left = COOKIES.manual_days_left()
            extra = f'，剩余 {left:.1f} 天' if left is not None else ''
            lines.append(f'　过期时间：{COOKIES.manual_expire_at}{extra}')
        else:
            lines.append('　过期时间：未知（微博登录 Cookie 一般 7 天左右）')
    else:
        lines.append('手工 Cookie：未设置（不需要也没问题）')

    if COOKIES.guest:
        left = COOKIES.guest_days_left()
        left_txt = f'剩余 {left:.1f} 天' if left is not None else '过期时间未知'
        lines.append(f'自动游客 Cookie：已获取（{left_txt}）')
        lines.append(f'　获取时间：{COOKIES.guest_fetched_at or "未知"}')
    else:
        lines.append('自动游客 Cookie：尚未获取（首次调用时自动申请）')

    ok, detail = await _validate_cookie(_pick_probe_uid(ev.group_id))
    lines.append(f'实时校验：{"✅ " + detail if ok else "❌ " + detail}')
    lines.append('说明：游客身份可读第 1 页（约 10 条），手工 Cookie 可翻更多页。')
    await bot.send(ev, '\n'.join(lines))


@sv.on_fullmatch(('清除cookie', '清除微博cookie', '删除微博cookie'))
async def clear_weibo_cookie(bot, ev: CQEvent):
    """清除手工 Cookie，改用自动游客身份（仅超级管理员）"""
    if not priv.check_priv(ev, priv.SUPERUSER):
        await bot.finish(ev, '仅超级管理员可执行该操作！')
    COOKIES.clear_manual()
    await COOKIES.refresh_guest()
    ok, detail = await _validate_cookie(_pick_probe_uid(ev.group_id))
    await bot.send(ev, f'已清除手工 Cookie，改用自动游客身份。\n'
                       f'校验：{"✅ " + detail if ok else "❌ " + detail}')


@sv.scheduled_job('cron', hour=10, minute=25)
async def scheduled_cookie_health():
    """每天巡检一次 Cookie：临近过期自动续期，彻底不可用时私聊主人"""
    try:
        await COOKIES.ensure_ready()
        ok, detail = await _validate_cookie()
        if ok:
            sv.logger.info(f"Cookie 巡检正常：{COOKIES.source()}（{detail}）")
            return
        sv.logger.error(f"Cookie 巡检失败：{detail}")
        await notify_superusers(
            f"[微博推送] Cookie 巡检失败：{detail}\n"
            f"当前使用：{COOKIES.source()}\n"
            "可用「微博cookie状态」查看详情，或「更新cookie [cookie字符串]」更新认证信息。"
        )
    except Exception as e:
        sv.logger.error(f"Cookie 巡检异常: {type(e).__name__}: {e}")

       
# 主动检查微博更新  
@sv.on_fullmatch(('检查微博更新', '检查微博', '微博检查'))  
async def manual_check_weibo(bot, ev: CQEvent):  
    """手动触发检查所有关注的微博更新"""  
    user_id = ev.user_id  
      
    # 频率限制检查  
    if not _nlmt.check(user_id):  
        await bot.finish(ev, '今日检查次数已达上限，请明天再试~')  
    if not flmt.check(user_id):  
        await bot.finish(ev, f'操作太频繁啦，请{int(flmt.left_time(user_id)) + 1}秒后再试~')  
      
    # 发送开始检查的消息  
    await bot.send(ev, '🔍 正在检查微博更新，请稍候...')  
      
    try:  
        # 调用核心检查函数  
        ran = await check_and_push_new_weibo()
        if ran is False:
            await bot.send(ev, '⏳ 已有一轮微博检查正在进行中，本次跳过，请稍后再试~')
        else:
            await bot.send(ev, '✅ 微博检查完成！如有新动态已推送至相关群组')  
    except Exception as e:  
        sv.logger.error(f"手动检查微博失败: {e}")  
        await bot.send(ev, f'❌ 检查过程中出现错误: {str(e)}')  
      
    # 更新频率限制  
    _nlmt.increase(user_id)  
    flmt.start_cd(user_id)       