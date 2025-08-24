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

sv = Service('微博推送', visible=True, enable_on_default=True, help_='微博推送服务')

# 配置文件路径
CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'weibo_config.json')

# 频率限制 - CD冷却10秒，每天20000次
flmt = FreqLimiter(10)
_nlmt = DailyNumberLimiter(20000)

# 配置结构：群独立黑名单
weibo_config = {
    'group_follows': {},      # {group_id: {weibo_id: {name: '微博名', last_post_id: '最后一条ID'}}}
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
    """加载配置文件"""
    global weibo_config
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            loaded_config = json.load(f)
            # 加载基础配置
            for key in ['group_follows', 'group_enable', 'account_cache']:
                weibo_config[key] = loaded_config.get(key, {})
            # 加载群黑名单（确保为集合类型）
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


# 初始化配置
load_config()

# -------------------------- 关键修复：补充完整请求头 --------------------------
# 1. 打开 https://m.weibo.cn/ 登录账号
# 2. F12打开开发者工具 → Network标签 → 刷新页面 → 选任意getIndex请求
# 3. 从Request Headers复制Cookie，提取XSRF-TOKEN值（Cookie中XSRF-TOKEN=xxx的xxx部分）
headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Referer': 'https://m.weibo.cn/',
    'X-Requested-With': 'XMLHttpRequest',
    'Cookie': 'SCF=AoSBzJSk8NJBjACijg-DwFAuzw0_-DIWQTOgTiaNVpeJPyhwC7ayyZjDZTGOdVcNXvv1wUsTt6C1Q5R7x0ujcSg.; SINAGLOBAL=2171047519112.6987.1754886255230; UOR=,,www.doubao.com; ULV=1755743469490:9:9:4:908131278112.565.1755743469408:1755690427342; SUB=_2A25Fo643DeRhGeBM6VIX9CjJzDqIHXVmwK__rDV8PUNbmtANLUGnkW9NROIKF5bwiSemfKlI2QD23VR6GvroVRu5; SUBP=0033WrSXqPxfM725Ws9jqgMF55529P9D9Whf44reS4FM0yqHofyYWyqT5JpX5KzhUgL.FoqEeo5cShqfS0q2dJLoIpMLxKnL12-LBo2LxK-LB--L1-x7MJL_9Btt; ALF=02_1758423911; XSRF-TOKEN=B5TW9C4hG6ZwoEfd8w9nkFU3; WBPSESS=GorFbSecsDrEKtrITJcA_6ilfbhSxLCNmTRnmt8pjpB5PZEy2Z-htIPCc_BlkelCdAKp9Cu2Q_RyxH-PyEHFaSRPBmQUlMyiH842WTpLA5hwiPSGs5De0BUBTrgVy0gO6prjXv6I5A4vmMD4EQ7_VA==',  # 必改：例：SUB=xxx; XSRF-TOKEN=xxx; ...
    'X-XSRF-TOKEN': 'B5TW9C4hG6ZwoEfd8w9nkFU3'  # 必改：例：abc123def456
}
# -----------------------------------------------------------------------------


async def get_weibo_user_info(uid, retry=2):
    """获取微博用户信息（带重试+格式校验）"""
    if not uid.isdigit():
        return None
    
    # 优先从缓存获取
    if uid in weibo_config['account_cache']:
        return weibo_config['account_cache'][uid]
    
    url = f'https://m.weibo.cn/api/container/getIndex?type=uid&value={uid}'
    for _ in range(retry + 1):
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(url, timeout=10) as resp:
                    # 校验响应是否为JSON
                    if 'application/json' not in resp.headers.get('Content-Type', ''):
                        sv.logger.warning(f"用户{uid}信息非JSON响应，重试中")
                        await asyncio.sleep(1)
                        continue
                    
                    data = await resp.json()
                    if data.get('ok') == 1:
                        user_info = data.get('data', {}).get('userInfo', {})
                        if not user_info:
                            return None
                        # 缓存用户信息
                        result = {
                            'name': user_info.get('screen_name', f'用户{uid}'),
                            'uid': uid
                        }
                        weibo_config['account_cache'][uid] = result
                        save_config()
                        return result
                    sv.logger.warning(f"用户{uid}信息获取失败，API返回: {data}")
                    await asyncio.sleep(1)
        except Exception as e:
            sv.logger.error(f"用户{uid}信息请求异常: {e}，重试中")
            await asyncio.sleep(1)
    
    sv.logger.error(f"用户{uid}信息获取失败（已达最大重试次数）")
    return None


async def get_weibo_user_latest_posts(uid, count=5, retry=2):
    """获取用户最新微博（带重试+格式校验+视频解析）"""
    url = f'https://m.weibo.cn/api/container/getIndex?type=uid&value={uid}&containerid=107603{uid}&page=1'
    for _ in range(retry + 1):
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(url, timeout=10) as resp:
                    # 校验响应是否为JSON
                    if 'application/json' not in resp.headers.get('Content-Type', ''):
                        resp_text = await resp.text()[:200]  # 打印前200字符排查
                        sv.logger.warning(f"微博{uid}非JSON响应: {resp_text}，重试中")
                        await asyncio.sleep(1)
                        continue
                    
                    data = await resp.json()
                    if data.get('ok') != 1:
                        sv.logger.warning(f"微博{uid}获取失败，API返回: {data}，重试中")
                        await asyncio.sleep(1)
                        continue
                    
                    # 解析微博内容
                    cards = data.get('data', {}).get('cards', [])
                    posts = []
                    for card in cards:
                        if card.get('card_type') != 9:  # 仅处理微博内容卡片
                            continue
                        
                        mblog = card.get('mblog', {})
                        retweeted_status = mblog.get('retweeted_status')  # 转发微博数据
                        
                        # 1. 处理正文
                        if retweeted_status:
                            # 转发微博：组合转发说明+原内容
                            retweeted_text = re.sub(r'<br\s*/?>', '\n', retweeted_status.get('text', ''))
                            retweeted_text = re.sub(r'<[^>]+>', '', retweeted_text)
                            retweeted_text = html.unescape(retweeted_text).strip() or "【被转发微博无正文】"
                            
                            forward_text = re.sub(r'<br\s*/?>', '\n', mblog.get('text', ''))
                            forward_text = re.sub(r'<[^>]+>', '', forward_text)
                            forward_text = html.unescape(forward_text).strip()
                            
                            # 过滤无意义转发说明
                            if re.match(r'^[\s!"#$%&\'()*+,-./:;<=>?@\[\\\]^_`{|}~，。、；：？！…—·《》「」『』【】（）]*$', forward_text):
                                forward_text = ''
                            
                            text = f"转发说明：{forward_text}\n\n被转发内容：{retweeted_text}" if forward_text else f"被转发内容：{retweeted_text}"
                        else:
                            # 原创微博：直接清洗正文
                            text = re.sub(r'<br\s*/?>', '\n', mblog.get('text', ''))
                            text = re.sub(r'<[^>]+>', '', text)
                            text = html.unescape(text).strip() or "【无正文内容】"
                        
                        # 2. 处理图片
                        pics = retweeted_status.get('pics', []) if retweeted_status else mblog.get('pics', [])
                        pic_urls = [pic.get('large', {}).get('url', '') for pic in pics if pic.get('large')]
                        
                        # 3. 处理视频
                        video_info = {'play_page_url': '', 'cover_url': ''}
                        page_info = retweeted_status.get('page_info', {}) if retweeted_status else mblog.get('page_info', {})
                        if page_info.get('type') in ['video', 'weibo_video']:
                            # 提取播放页链接
                            fid = page_info.get('fid') or page_info.get('object_id')
                            if fid:
                                video_info['play_page_url'] = f"https://video.weibo.com/show?fid={fid}"
                            # 提取封面链接（多来源 fallback）
                            video_info['cover_url'] = (
                                page_info.get('page_pic', {}).get('url', '') or
                                page_info.get('media_info', {}).get('cover_image_url', '') or
                                page_info.get('media_info', {}).get('stream_url_hd', '').replace('.mp4', '.jpg').replace('.webm', '.jpg')
                            )
                        # 视频信息追加到正文
                        if video_info['play_page_url']:
                            if video_info['cover_url']:
                                text += f"\n[CQ:image,url={escape(video_info['cover_url'])}]"
                            text += f"\n🎬 视频播放页：{video_info['play_page_url']}"
                        
                        # 4. 处理时间和统计数据
                        raw_time = retweeted_status.get('created_at', '未知时间') if retweeted_status else mblog.get('created_at', '未知时间')
                        formatted_time = format_weibo_time(raw_time)
                        
                        stats = retweeted_status if retweeted_status else mblog
                        posts.append({
                            'id': mblog.get('id', ''),
                            'text': text,
                            'pics': pic_urls,
                            'video': video_info,
                            'created_at': formatted_time,
                            'reposts_count': stats.get('reposts_count', 0),
                            'comments_count': stats.get('comments_count', 0),
                            'attitudes_count': stats.get('attitudes_count', 0)
                        })
                        
                        if len(posts) >= count:
                            break
                    
                    return posts
        except Exception as e:
            sv.logger.error(f"微博{uid}请求异常: {e}，重试中")
            await asyncio.sleep(1)
    
    sv.logger.error(f"微博{uid}获取失败（已达最大重试次数）")
    return []


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
            
            # 按ID倒序，取最新一条
            latest_post = max(latest_posts, key=lambda x: x['id'])
            latest_post_id = latest_post['id']
            
            # 筛选需要推送的群（已开启推送+未推送过该条）
            groups_to_push = []
            for group_id, follows in weibo_config['group_follows'].items():
                if (uid in follows and 
                    weibo_config['group_enable'].get(group_id, True) and 
                    latest_post_id > follows[uid]['last_post_id']):
                    groups_to_push.append(group_id)
                    weibo_config['group_follows'][group_id][uid]['last_post_id'] = latest_post_id
            
            # 推送并保存配置
            if groups_to_push:
                save_config()
                user_info = await get_weibo_user_info(uid)
                user_name = user_info['name'] if user_info else f'用户{uid}'
                await push_weibo_to_groups(groups_to_push, user_name, uid, latest_post)
        
        except Exception as e:
            sv.logger.error(f"处理微博{uid}时出错: {e}")
            continue
    
    sv.logger.info("微博更新检查完成")


async def push_weibo_to_groups(group_ids, name, uid, post):
    """推送微博到指定群"""
    # 组装消息
    msg_parts = [
        f"📢 {name} (ID: {uid}) 发布新微博：\n",
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
            await asyncio.sleep(0.5)
        except Exception as e:
            sv.logger.error(f"向群{group_id}推送失败: {e}，消息预览: {full_msg[:200]}...")


# -------------------------- 定时任务（调整为5分钟减少反爬） --------------------------
@sv.scheduled_job('interval', minutes=5)
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
    
    uid = ev.message.extract_plain_text().strip()
    if not uid:
        await bot.finish(ev, '请输入要关注的微博ID哦~')
    
    # 检查是否在本群黑名单中
    group_blacklist = weibo_config['group_blacklist'].get(group_id, set())
    if uid in group_blacklist:
        await bot.finish(ev, f'该微博ID({uid})已在本群黑名单中，禁止关注~')
    
    # 验证微博ID有效性
    user_info = await get_weibo_user_info(uid)
    if not user_info:
        await bot.finish(ev, f'未查询到微博ID为{uid}的用户，请检查ID是否正确~')
    
    if group_id not in weibo_config['group_follows']:
        weibo_config['group_follows'][group_id] = {}
    
    if uid in weibo_config['group_follows'][group_id]:
        name = weibo_config['group_follows'][group_id][uid]['name']
        await bot.finish(ev, f'本群已经关注过 {name} 啦~')
    
    latest_posts = await get_weibo_user_latest_posts(uid, 1)
    last_post_id = latest_posts[0]['id'] if latest_posts else ''
    
    weibo_config['group_follows'][group_id][uid] = {
        'name': user_info['name'],
        'last_post_id': last_post_id
    }
    
    if group_id not in weibo_config['group_enable']:
        weibo_config['group_enable'][group_id] = True
    
    save_config()
    _nlmt.increase(user_id)
    flmt.start_cd(user_id)
    await bot.send(ev, f'本群成功关注 {user_info["name"]} 的微博啦~ 有新动态会第一时间通知哦~')

# 全群关注微博功能
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
    
    uid = ev.message.extract_plain_text().strip()
    if not uid:
        await bot.finish(ev, '请输入要全群关注的微博ID哦~')
    
    # 验证微博ID有效性
    user_info = await get_weibo_user_info(uid)
    if not user_info:
        await bot.finish(ev, f'未查询到微博ID为{uid}的用户，请检查ID是否正确~')
    
    # 获取所有已加入的群
    groups = await bot.get_group_list()
    if not groups:
        await bot.finish(ev, '未加入任何群组，无法进行全群关注~')
    
    latest_posts = await get_weibo_user_latest_posts(uid, 1)
    last_post_id = latest_posts[0]['id'] if latest_posts else ''
    
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
                'name': user_info['name'],
                'last_post_id': last_post_id
            }
            new_follow_count += 1
        
        # 确保开启推送
        weibo_config['group_enable'][group_id] = True
    
    save_config()
    _nlmt.increase(user_id)
    flmt.start_cd(user_id)
    await bot.send(ev, f'成功为{new_follow_count}个群开启 {user_info["name"]} 的微博关注~ 有新动态会第一时间通知哦~')

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
    help_msg = '''微博推送插件帮助：
- 关注微博 [微博ID]：关注指定微博账号（仅本群生效）
- 全群关注微博 [微博ID]：所有已加入的群都关注并开启推送（管理员）
- 取消关注微博 [微博ID]：取消关注指定微博账号（仅本群生效）
- 查看关注的微博：查看本群已关注的微博账号
- 微博推送开关 [on/off]：开启或关闭本群微博推送（管理员）
- 微博黑名单 [ID]：将指定微博ID加入本群黑名单（管理员）
- 微博黑名单移除 [ID]：将指定微博ID从本群黑名单移除（管理员）
- 查看微博黑名单：查看本群黑名单中的微博ID（管理员）
注：微博ID是指微博的数字ID，不是昵称哦~'''
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

# @on_startup
# async def startup_check():
    # sv.logger.info("微博推送插件已启动，正在进行首次微博检查...")
    # await asyncio.sleep(10)  # 延迟检查，避免启动冲突
    # await check_and_push_new_weibo()
