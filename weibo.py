from hoshino import Service, priv  
from hoshino.typing import CQEvent  
from hoshino.util import DailyNumberLimiter, FreqLimiter, escape    
import json  
import os  
import asyncio  
import aiohttp  
import re  
import html  
from datetime import datetime    
from nonebot import on_startup  
import requests  
from lxml import etree  
import time  
import random  
  
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
  
def format_weibo_time(raw_time):  
    """时间格式转换为YYYY-MM-DD HH:MM:SS"""  
    try:  
        dt = datetime.strptime(raw_time, '%a %b %d %H:%M:%S %z %Y')  
        return dt.strftime('%Y-%m-%d %H:%M:%S')  
    except Exception as e:  
        sv.logger.warning(f"时间格式化失败: {e}，原始时间: {raw_time}")  
        return raw_time    
  
def load_config():  
    """加载配置文件（带向后兼容）"""  
    global weibo_config  
    if os.path.exists(CONFIG_PATH):  
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:  
            loaded_config = json.load(f)  
              
            # 加载基础配置  
            for key in ['group_follows', 'group_enable', 'account_cache']:  
                weibo_config[key] = loaded_config.get(key, {})  
              
            # 迁移：将last_post_id转换为last_post_time  
            for group_id, follows in weibo_config['group_follows'].items():  
                for uid, info in follows.items():  
                    if 'last_post_id' in info and 'last_post_time' not in info:  
                        # 旧版本，需要迁移  
                        info['last_post_time'] = ''  # 重置为空，会重新获取  
                        del info['last_post_id']  
              
            # 加载群黑名单  
            weibo_config['group_blacklist'] = {}  
            for group_id, uids in loaded_config.get('group_blacklist', {}).items():  
                weibo_config['group_blacklist'][group_id] = set(uids)  
    else:  
        save_config()  
  
def save_config():  
    """保存配置文件（集合转列表适配JSON序列化）"""  
    config_to_save = weibo_config.copy()  
    config_to_save['group_blacklist'] = {  
        group_id: list(uids) for group_id, uids in weibo_config['group_blacklist'].items()  
    }  
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:  
        json.dump(config_to_save, f, ensure_ascii=False, indent=2)  
  
# 初始化数据文件和headers  
def init_data():  
    # 确保数据文件存在  
    if not os.path.exists(DATA_FILE):  
        with open(DATA_FILE, 'w', encoding='utf-8') as f:  
            json.dump({  
                'cookie': '',  
                'xsrf_token': ''  
            }, f, ensure_ascii=False, indent=2)  
        return {'cookie': '', 'xsrf_token': ''}  
      
    # 读取现有数据  
    try:  
        with open(DATA_FILE, 'r', encoding='utf-8') as f:  
            return json.load(f)  
    except:  
        # 数据文件损坏时重建  
        with open(DATA_FILE, 'w', encoding='utf-8') as f:  
            json.dump({  
                'cookie': '',  
                'xsrf_token': ''  
            }, f, ensure_ascii=False, indent=2)  
        return {'cookie': '', 'xsrf_token': ''}  
  
# 初始化数据  
data = init_data()  
# 初始化配置  
load_config()  
# -------------------------- 关键修复：补充完整请求头 --------------------------  
# 1. 打开 https://m.weibo.cn/ 登录账号  
# 2. F12打开开发者工具 → Network标签 → 刷新页面 → 选任意getIndex请求  
# 3. 从Request Headers复制Cookie，提取XSRF-TOKEN值（Cookie中XSRF-TOKEN=xxx的xxx部分）  
headers = {  
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',  
    'Cache-Control': 'max-age=0',  
    'Upgrade-Insecure-Requests': '1',  
    'Sec-Fetch-Dest': 'document',  
    'Sec-Fetch-Mode': 'navigate',  
    'Sec-Fetch-Site': 'none',  
    'Sec-Fetch-User': '?1',  
    # 移除 X-Requested-With  
}
# -----------------------------------------------------------------------------  
  
def format_weibo_time(time_text):  
    """将微博时间文本标准化为 YYYY-MM-DD HH:MM:SS 格式"""  
    import re  
    from datetime import datetime, timedelta  
      
    if not time_text or time_text == 'unknown':  
        return ''  
      
    try:  
        # 处理相对时间（如"3分钟前"、"1小时前"等）  
        if '分钟前' in time_text:  
            minutes = int(re.search(r'(\d+)分钟前', time_text).group(1))  
            dt = datetime.now() - timedelta(minutes=minutes)  
            return dt.strftime('%Y-%m-%d %H:%M:%S')  
        elif '小时前' in time_text:  
            hours = int(re.search(r'(\d+)小时前', time_text).group(1))  
            dt = datetime.now() - timedelta(hours=hours)  
            return dt.strftime('%Y-%m-%d %H:%M:%S')  
        elif '今天' in time_text:  
            # 格式如"今天 12:30"  
            time_part = re.search(r'今天 (\d{2}:\d{2})', time_text).group(1)  
            dt = datetime.now().strftime('%Y-%m-%d') + ' ' + time_part + ':00'  
            return dt  
        elif '昨天' in time_text:  
            # 格式如"昨天 12:30"  
            time_part = re.search(r'昨天 (\d{2}:\d{2})', time_text).group(1)  
            dt = datetime.now() - timedelta(days=1)  
            return dt.strftime('%Y-%m-%d') + ' ' + time_part + ':00'  
        elif '月' in time_text and '日' in time_text:  
            # 格式如"12月25日 12:30" 或 "12月25日"  
            match = re.search(r'(\d{1,2})月(\d{1,2})日(?: (\d{2}:\d{2}))?', time_text)  
            if match:  
                month = match.group(1)  
                day = match.group(2)  
                time_part = match.group(3) if match.group(3) else '00:00'  
                year = datetime.now().year  
                return f'{year}-{month.zfill(2)}-{day.zfill(2)} {time_part}:00'  
        elif '-' in time_text:  
            # 格式如"2024-12-25 12:30:00"  
            if len(time_text.split(' ')) == 2 and ':' in time_text.split(' ')[1]:  
                return time_text  
            else:  
                # 可能只有日期部分  
                return time_text + ' 00:00:00'  
          
        # 如果都不匹配，返回原文本  
        return time_text  
    except Exception as e:  
        sv.logger.warning(f"时间格式化失败: {time_text}, 错误: {e}")  
        return time_text  
  
def parse_html_response(html_content):  
    """解析HTML响应，提取微博内容"""  
    try:  
        from lxml import etree  
        import re  
              
        # 移除XML声明  
        if html_content.startswith('<?xml'):  
            html_content = re.sub(r'<\?xml[^>]*\?>', '', html_content)  
              
        # 解析HTML  
        selector = etree.HTML(html_content)  
        if selector is None:  
            sv.logger.error("HTML解析失败：selector为None")  
            return []  
              
        # 查找所有微博卡片  
        cards = selector.xpath('//div[@class="c" and starts-with(@id, "M_")]')  
        all_posts = []  
              
        for card in cards:  
            try:  
                # 提取微博ID  
                card_id = card.get('id', '')  
                post_id = card_id.replace('M_', '') if card_id else 'unknown'  
                      
                # 提取文本内容  
                text_parts = []  
                text_nodes = card.xpath('.//span[@class="ctt"]/text()')  
                for node in text_nodes:  
                    if node and node.strip():  
                        text_parts.append(node.strip())  
                      
                if not text_parts:  
                    all_text = card.xpath('.//text()')  
                    for text in all_text:  
                        text = text.strip()  
                        if text and not any(x in text for x in ['转发', '评论', '赞', '来自', '原文链接']):  
                            text_parts.append(text)  
                      
                text = '\n'.join(text_parts) if text_parts else "【无正文内容】" 
                      
                # 针对多图微博的专门提取逻辑  
                pic_urls = []  
                found_imgs = set()  
                    
                sv.logger.info(f"微博 {post_id} 开始多图专项分析")  
                    
                # 策略1：查找所有包含图片的链接（多图通常在多个a标签中）  
                img_links = card.xpath('.//a[.//img]')  
                sv.logger.info(f"微博 {post_id} 找到 {len(img_links)} 个包含图片的链接")  
                    
                for i, link in enumerate(img_links):  
                    # 获取链接中的所有图片  
                    link_imgs = link.xpath('.//img')  
                    for img in link_imgs:  
                        src = img.get('src', '')  
                        # 放宽图片过滤条件  
                        if src and any(ext in src.lower() for ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp']):  
                            # 简化域名检查  
                            if 'sinaimg' in src.lower():  
                                # 仅过滤明显的非内容图片  
                                if not any(x in src.lower() for x in ['h5.sinaimg.cn', '/upload/', 'avatar', 'profile']):  
                                    found_imgs.add(src)
                                    sv.logger.info(f"链接{i}中发现图片: {src}")  
                    
                # 策略2：查找可能的图片组容器  
                possible_containers = [    
                    './/div[contains(@class, "media")]//img',  
                    './/div[contains(@class, "gallery")]//img',     
                    './/div[contains(@class, "photos")]//img',  
                    './/div[contains(@class, "img")]//img',  
                    './/span[contains(@class, "ib")]//img',  # 基于日志中的class='ib'  
                ]  
                    
                for container_query in possible_containers:  
                    container_imgs = card.xpath(container_query)  
                    sv.logger.info(f"容器查询 '{container_query}' 找到 {len(container_imgs)} 个图片")  
                        
                    for img in container_imgs:  
                        src = img.get('src', '')  
                        if src and any(ext in src.lower() for ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp']):  
                            if any(domain in src for domain in ['sinaimg.cn', 'wx1.sinaimg.cn', 'wx2.sinaimg.cn', 'wx3.sinaimg.cn', 'wx4.sinaimg.cn']):  
                                src_lower = src.lower()  
                                if 'h5.sinaimg.cn' not in src_lower and '/upload/' not in src_lower:  
                                    found_imgs.add(src)  
                    
                # 策略3：查找所有img标签（备用）  
                all_imgs = card.xpath('.//img')  
                sv.logger.info(f"备用查询找到 {len(all_imgs)} 个img标签")  
                    
                for img in all_imgs:  
                    src = img.get('src', '')  
                    if src and any(ext in src.lower() for ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp']):  
                        if any(domain in src for domain in ['sinaimg.cn', 'wx1.sinaimg.cn', 'wx2.sinaimg.cn', 'wx3.sinaimg.cn', 'wx4.sinaimg.cn']):  
                            src_lower = src.lower()  
                            if 'h5.sinaimg.cn' not in src_lower and '/upload/' not in src_lower:  
                                found_imgs.add(src)  
                    
                # 转换为原图URL  
                for src in found_imgs:  
                    original_url = src.replace('/wap180/', '/large/').replace('/thumb/', '/large/').replace('/bmiddle/', '/large/')  
                    if original_url.startswith('//'):  
                        original_url = 'https:' + original_url  
                    if original_url not in pic_urls:  
                        pic_urls.append(original_url)  
                    
                sv.logger.info(f"微博 {post_id} 最终提取到 {len(pic_urls)} 张图片")  
                      
                # 提取时间  
                time_elem = card.xpath('.//span[@class="ct"]')  
                time_text = time_elem[0].text if time_elem else 'unknown'  
                  
                # 标准化时间格式  
                formatted_time = format_weibo_time(time_text)  
                      
                all_posts.append({  
                    'id': post_id,  
                    'text': text,  
                    'pics': pic_urls,  
                    'video': {'play_page_url': '', 'cover_url': ''},  
                    'created_at': time_text,  
                    'created_time': formatted_time,  # 新增标准化时间字段  
                    'reposts_count': 0,  
                    'comments_count': 0,  
                    'attitudes_count': 0  
                })  
                      
            except Exception as e:  
                sv.logger.error(f"解析单个微博卡片失败: {e}")  
                continue  
              
        return all_posts  
              
    except Exception as e:  
        sv.logger.error(f"HTML解析失败: {e}")  
        return []
        
async def get_weibo_user_info(uid, retry=2, force_refresh=False):  
    """获取微博用户信息（带重试+格式校验+强制刷新）"""  
    if not uid.isdigit():  
        return None  
      
    # 强制刷新时清除缓存  
    if force_refresh and uid in weibo_config['account_cache']:  
        del weibo_config['account_cache'][uid]  
        save_config()  
      
    # 优先从缓存获取  
    if uid in weibo_config['account_cache']:  
        cached_info = weibo_config['account_cache'][uid]  
        # 验证缓存的UID是否匹配  
        if cached_info.get('uid') == uid:  
            # 确保缓存中有有效的用户名  
            if not cached_info.get('name'):  
                cached_info['name'] = f'用户{uid}'  
                weibo_config['account_cache'][uid] = cached_info  
                save_config()  
            return cached_info  
        else:  
            # 缓存不匹配，清除并重新获取  
            sv.logger.warning(f"缓存UID不匹配，清除缓存: 缓存={cached_info.get('uid')}, 请求={uid}")  
            del weibo_config['account_cache'][uid]  
            save_config()  
      
    url = f'https://m.weibo.cn/api/container/getIndex?type=uid&value={uid}'  
    for attempt in range(retry + 1):  
        try:  
            async with aiohttp.ClientSession(headers=headers) as session:  
                async with session.get(url, timeout=10) as resp:  
                    # 校验响应是否为JSON  
                    if 'application/json' not in resp.headers.get('Content-Type', ''):  
                        sv.logger.warning(f"用户{uid}信息非JSON响应(尝试{attempt+1}/{retry+1})，重试中")  
                        await asyncio.sleep(3)  
                        continue  
                      
                    data = await resp.json()  
                    if data.get('ok') == 1:  
                        user_info = data.get('data', {}).get('userInfo', {})  
                        if not user_info:  
                            sv.logger.warning(f"用户{uid}信息为空，API返回: {data}")  
                            # 返回默认用户信息而不是None  
                            result = {  
                                'name': f'用户{uid}',  
                                'uid': uid  
                            }  
                            weibo_config['account_cache'][uid] = result  
                            save_config()  
                            return result  
                          
                        # 缓存用户信息  
                        result = {  
                            'name': user_info.get('screen_name', f'用户{uid}'),  
                            'uid': uid  
                        }  
                          
                        # 确保用户名不为空  
                        if not result['name'] or result['name'].strip() == '':  
                            result['name'] = f'用户{uid}'  
                            sv.logger.warning(f"用户{uid}获取到空用户名，使用默认值")  
                          
                        weibo_config['account_cache'][uid] = result  
                        save_config()  
                        sv.logger.info(f"成功获取用户{uid}信息: {result['name']}")  
                        return result  
                      
                    sv.logger.warning(f"用户{uid}信息获取失败(尝试{attempt+1}/{retry+1})，API返回: {data}")  
                    if attempt < retry:  
                        await asyncio.sleep(3)  
                      
        except Exception as e:  
            sv.logger.error(f"用户{uid}信息请求异常(尝试{attempt+1}/{retry+1}): {e}")  
            if attempt < retry:  
                await asyncio.sleep(3)  
      
    # 所有重试失败后，返回默认用户信息  
    sv.logger.error(f"用户{uid}信息获取失败（已达最大重试次数），使用默认用户名")  
    result = {  
        'name': f'用户{uid}',  
        'uid': uid  
    }  
    weibo_config['account_cache'][uid] = result  
    save_config()  
    return result
    
async def get_weibo_user_latest_posts(uid, count=5, retry=2):  
    """获取用户最新微博(HTML抓取版本)"""  
    global data  # Add this line  
    all_posts = []  
    page = 1  
    max_pages = 5  
      
    # HTML请求头（模拟浏览器）  
    html_headers = {  
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36',  
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',  
        'Accept-Language': 'zh-CN,zh;q=0.9,ja;q=0.8,zh-TW;q=0.7',  
        'Accept-Encoding': 'gzip, deflate, br, zstd',  
        'Cache-Control': 'max-age=0',  
        'Cookie': data['cookie'],  # This line was causing the error  
        'Upgrade-Insecure-Requests': '1',  
        'Sec-Fetch-Dest': 'document',  
        'Sec-Fetch-Mode': 'navigate',  
        'Sec-Fetch-Site': 'none',  
        'Sec-Fetch-User': '?1'  
    }
      
    while len(all_posts) < count and page <= max_pages:  
        url = f'https://weibo.cn/{uid}?page={page}'  
          
        for attempt in range(retry + 1):  
            try:  
                async with aiohttp.ClientSession(headers=html_headers) as session:  
                    async with session.get(url, timeout=10) as resp:  
                        if resp.status != 200:  
                            sv.logger.warning(f"微博{uid}页面请求失败(页{page},尝试{attempt+1}/{retry+1}) - 状态码: {resp.status}")  
                            await asyncio.sleep(3)  
                            continue  
                          
                        content_type = resp.headers.get('Content-Type', '')  
                        if 'text/html' in content_type:  
                            # HTML响应处理  
                            html_content = await resp.text()  
                            sv.logger.info(f"获取到HTML内容，长度: {len(html_content)}")  
                            html_posts = parse_html_response(html_content)  
                            sv.logger.info(f"HTML解析结果: {len(html_posts)}条微博")  
                            if html_posts:  
                                all_posts.extend(html_posts)  
                                if len(all_posts) >= count:  
                                    return all_posts[:count]  
                            break  
                        elif 'application/json' in content_type:  
                            # JSON响应处理（备用）  
                            data = await resp.json()  
                            if data.get('ok') == 1:  
                                cards = data.get('data', {}).get('cards', [])  
                                for card in cards:  
                                    if card.get('card_type') == 9:  
                                        mblog = card.get('mblog', {})  
                                        # 原有的JSON解析逻辑...  
                                        if len(all_posts) >= count:  
                                            return all_posts  
                                break  
                            else:  
                                sv.logger.warning(f"微博{uid}获取失败(页{page},尝试{attempt+1}/{retry+1}), API返回: {data}")  
                                await asyncio.sleep(3)  
                        else:  
                            sv.logger.warning(f"微博{uid}未知响应格式(页{page},尝试{attempt+1}/{retry+1}) - Content-Type: {content_type}")  
                            await asyncio.sleep(3)  
                            continue  
                              
            except Exception as e:  
                sv.logger.error(f"微博{uid}请求异常(页{page},尝试{attempt+1}/{retry+1}): {type(e).__name__}: {e}")  
                await asyncio.sleep(3)  
          
        page += 1  
        await asyncio.sleep(1)  
      
    return all_posts


async def check_and_push_new_weibo():  
    """检查新微博并推送"""  
    sv.logger.info("开始检查微博更新...")  
    all_followed_uids = set()  
    # 收集所有已关注的微博ID  
    for follows in weibo_config['group_follows'].values():  
        all_followed_uids.update(follows.keys())  
          
    for uid in all_followed_uids:  
        try:  
            latest_posts = await get_weibo_user_latest_posts(uid)  
            if not latest_posts:  
                continue  
                  
            # 获取该用户在各群的最早 last_post_time(用于筛选新微博)  
            min_last_post_time = ''  
            for group_id, follows in weibo_config['group_follows'].items():  
                if uid in follows:  
                    current_time = follows[uid].get('last_post_time', '')  
                    if not min_last_post_time or current_time < min_last_post_time:  
                        min_last_post_time = current_time  
                
            # 筛选出所有新微博（使用时间比较）  
            new_posts = [post for post in latest_posts if post['created_time'] > min_last_post_time]  
                  
            if not new_posts:  
                continue  
                  
            # 按时间排序(从旧到新)  
            new_posts.sort(key=lambda x: x['created_time'])  
              
            # 收集所有需要更新的群  
            all_groups_to_update = set()  
              
            # 推送每一条新微博  
            for post in new_posts:  
                groups_to_push = []  
                for group_id, follows in weibo_config['group_follows'].items():  
                    if (uid in follows and   
                        weibo_config['group_enable'].get(group_id, True) and   
                        post['created_time'] > follows[uid].get('last_post_time', '')):  
                        groups_to_push.append(group_id)  
                        all_groups_to_update.add(group_id)  
                          
                if groups_to_push:  
                    # 强制刷新用户信息确保准确性  
                    user_info = await get_weibo_user_info(uid)  
                    user_name = user_info['name'] if user_info else f'用户{uid}'  
                        
                    # 验证UID匹配  
                    if user_info and user_info.get('uid') != uid:  
                        sv.logger.warning(f"用户信息不匹配: 请求UID={uid}, 返回UID={user_info.get('uid')}")  
                        # 如果不匹配，重新获取一次  
                        user_info = await get_weibo_user_info(uid, force_refresh=True)  
                        user_name = user_info['name'] if user_info else f'用户{uid}'  
                            
                    await push_weibo_to_groups(groups_to_push, user_name, uid, post)  
                              
            # 所有微博推送完毕后，统一更新为当前时间  
            if all_groups_to_update:  
                current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')  
                for group_id in all_groups_to_update:  
                    weibo_config['group_follows'][group_id][uid]['last_post_time'] = current_time  
                save_config()  
                  
        except Exception as e:  
            sv.logger.error(f"处理微博{uid}时出错: {e}")  
            continue

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
      
    # 追加图片  
    for pic_url in post['pics']:  
        if pic_url:  
            msg_parts.append(f"[CQ:image,url={escape(pic_url)}]\n")  
      
    # 追加统计和链接  
    msg_parts.extend([  
        f"\n👍 {post['attitudes_count']}  🔁 {post['reposts_count']}  💬 {post['comments_count']}",  
        f"\n发布时间：{post['created_at']}",  
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


# -------------------------- 定时任务（调整为10分钟减少反爬） --------------------------
@sv.scheduled_job('interval', minutes=10)
async def scheduled_check_weibo():
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
- 更新cookie + cookie  
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
    posts = await get_weibo_user_latest_posts(uid, count=5)  
    if not posts:  
        await bot.finish(ev, f'{user_info["name"]} 暂无微博内容~')  
      
    # 组装消息  
    msg_parts = [f'📱 {user_info["name"]} (ID: {uid}) 的最新{len(posts)}条微博:\n\n']  
      
    for i, post in enumerate(posts, 1):  
        msg_parts.append(f'【{i}】{post["text"][:100]}...\n' if len(post["text"]) > 100 else f'【{i}】{post["text"]}\n')  
          
        # 添加图片  
        for pic_url in post['pics'][:3]:  # 每条最多显示3张图  
            if pic_url:  
                msg_parts.append(f'[CQ:image,url={escape(pic_url)}]')  
          
        msg_parts.append(f'\n👍 {post["attitudes_count"]}  🔁 {post["reposts_count"]}  💬 {post["comments_count"]}')  
        msg_parts.append(f'\n发布时间: {post["created_at"]}')  
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
        posts = await get_weibo_user_latest_posts(uid, count=70)  
        if not posts:  
            await bot.finish(ev, '❌ 无法获取微博内容\n'  
                              '💡 可能是网络问题或认证失效，建议管理员检查配置')  
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
          
        # 添加图片  
        for pic_url in biweekly_post['pics']:  
            if pic_url:  
                msg_parts.append(f"[CQ:image,url={escape(pic_url)}]\n")  
          
        # 添加统计和链接  
        msg_parts.extend([  
            f"\n👍 {biweekly_post['attitudes_count']}  🔁 {biweekly_post['reposts_count']}  💬 {biweekly_post['comments_count']}",  
            f"\n发布时间：{biweekly_post['created_at']}",  
            f"\n原文链接：https://m.weibo.cn/status/{biweekly_post['id']}"  
        ])  
          
        _nlmt.increase(user_id)  
        flmt.start_cd(user_id)  
        await bot.send(ev, ''.join(msg_parts))  
          
    except Exception as e:  
        sv.logger.error(f"获取官方半月刊失败: {e}")  
        await bot.finish(ev, f'❌ 获取半月刊时发生错误: {str(e)}\n'  
                          '💡 请稍后重试或联系管理员处理')

@sv.on_prefix(('更新cookie',))
async def update_cookie(bot, ev: CQEvent):
    # 仅允许管理员操作
    if not priv.check_priv(ev, priv.ADMIN):
        await bot.finish(ev, '只有管理员才能更新Cookie哦~')
    
    new_cookie = ev.message.extract_plain_text().strip()
    if not new_cookie:
        await bot.finish(ev, '请输入完整的Cookie内容哦~')
    
    # 尝试从Cookie中提取XSRF-TOKEN
    xsrf_token = None
    for part in new_cookie.split(';'):
        part = part.strip()
        if part.startswith('XSRF-TOKEN='):
            xsrf_token = part.split('=', 1)[1]
            break
    
    if not xsrf_token:
        await bot.finish(ev, '未从Cookie中找到XSRF-TOKEN，请检查Cookie格式是否正确~')
    
    # 更新全局headers
    global headers
    headers['Cookie'] = new_cookie
    headers['X-XSRF-TOKEN'] = xsrf_token
    
    # 保存到数据文件（持久化）
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump({
            'cookie': new_cookie,
            'xsrf_token': xsrf_token
        }, f, ensure_ascii=False, indent=2)
    
    # 测试新配置是否有效
    test_uid = '6220646576'  # 新浪新闻的微博ID，用于测试
    test_result = await get_weibo_user_info(test_uid, retry=1)
    if test_result:
        await bot.send(ev, f'新配置测试成功，已获取到测试账号信息：{test_result["name"]}')
    else:
        await bot.send(ev, '新配置测试失败，可能Cookie已过期或格式错误，请重新检查~')
       
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
        await check_and_push_new_weibo()  
        await bot.send(ev, '✅ 微博检查完成！如有新动态已推送至相关群组')  
    except Exception as e:  
        sv.logger.error(f"手动检查微博失败: {e}")  
        await bot.send(ev, f'❌ 检查过程中出现错误: {str(e)}')  
      
    # 更新频率限制  
    _nlmt.increase(user_id)  
    flmt.start_cd(user_id)       