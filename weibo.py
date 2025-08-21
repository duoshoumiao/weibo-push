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

# 频率限制 - CD冷却，每天20000次
flmt = FreqLimiter(10)
_nlmt = DailyNumberLimiter(20000)

#在配置中添加黑名单
weibo_config = {
    'group_follows': {},      # {group_id: {weibo_id: {name: '微博名', last_post_id: '最后一条ID'}}}
    'group_enable': {},       # {group_id: True/False}
    'account_cache': {},      # {weibo_id: {name: '微博名', uid: '微博ID'}}
    'blacklist': set()        # 新增：存储被禁止关注的微博ID
}


def format_weibo_time(raw_time):
    """将微博原始时间格式转换为YYYY-MM-DD HH:MM:SS格式"""
    try:
        dt = datetime.strptime(raw_time, '%a %b %d %H:%M:%S %z %Y')
        return dt.strftime('%Y-%m-%d %H:%M:%S')
    except Exception as e:
        sv.logger.warning(f"时间格式化失败: {e}，原始时间: {raw_time}")
        return raw_time  

def load_config():
    global weibo_config
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            loaded_config = json.load(f)
            # 处理原有字段
            for key in ['group_follows', 'group_enable', 'account_cache']:
                weibo_config[key] = loaded_config.get(key, {})
            # 处理黑名单，确保是集合类型
            weibo_config['blacklist'] = set(loaded_config.get('blacklist', []))
    else:
        save_config()

def save_config():
    # 转换集合为列表以便JSON序列化
    config_to_save = weibo_config.copy()
    config_to_save['blacklist'] = list(weibo_config['blacklist'])
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(config_to_save, f, ensure_ascii=False, indent=2)

# 初始化加载配置
load_config()

headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Referer': 'https://m.weibo.cn/',
    'X-Requested-With': 'XMLHttpRequest'
}

# 获取微博用户信息
async def get_weibo_user_info(uid):
    if not uid.isdigit():
        return None  # 无效ID返回None
        
    if uid in weibo_config['account_cache']:
        return weibo_config['account_cache'][uid]
    
    url = f'https://m.weibo.cn/api/container/getIndex?type=uid&value={uid}'
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, timeout=10) as resp:
                data = await resp.json()
                if data.get('ok') == 1:
                    user_info = data.get('data', {}).get('userInfo', {})
                    # 确保获取到有效用户信息
                    if not user_info:
                        return None
                    result = {
                        'name': user_info.get('screen_name', f'用户{uid}'),
                        'uid': uid
                    }
                    weibo_config['account_cache'][uid] = result
                    save_config()
                    return result
                sv.logger.warning(f"获取用户{uid}信息失败，API返回: {data}")
                return None  # 获取失败返回None
    except Exception as e:
        sv.logger.error(f"获取微博用户信息失败: {e}")
        return None  # 异常情况返回None

# 获取微博用户最新微博（重点修复视频链接和封面解析）
async def get_weibo_user_latest_posts(uid, count=5):
    url = f'https://m.weibo.cn/api/container/getIndex?type=uid&value={uid}&containerid=107603{uid}&page=1'
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, timeout=10) as resp:
                data = await resp.json()
                if data.get('ok') != 1:
                    sv.logger.warning(f"获取用户{uid}微博失败，API返回: {data}")
                    return []
                
                cards = data.get('data', {}).get('cards', [])
                posts = []
                
                for card in cards:
                    if card.get('card_type') == 9:  # 微博内容卡片
                        mblog = card.get('mblog', {})
                        
                        # 处理微博正文（区分原创和转发）
                        raw_text = mblog.get('text', '')
                        retweeted_status = mblog.get('retweeted_status')  # 被转发的微博数据
                        
                        if retweeted_status:
                            # 提取被转发微博的正文
                            retweeted_raw_text = retweeted_status.get('text', '')
                            retweeted_text = re.sub(r'<br\s*/?>', '\n', retweeted_raw_text)
                            retweeted_text = re.sub(r'<[^>]+>', '', retweeted_text)
                            retweeted_text = html.unescape(retweeted_text)
                            retweeted_text = retweeted_text.strip() or "【被转发微博无正文】"
                            
                            # 处理转发者添加的内容（过滤无意义内容）
                            forward_text = re.sub(r'<br\s*/?>', '\n', raw_text)
                            forward_text = re.sub(r'<[^>]+>', '', forward_text)
                            forward_text = html.unescape(forward_text)
                            forward_text = forward_text.strip()
                            
                            # 过滤无意义内容
                            meaningless_pattern = re.compile(r'^[\s!"#$%&\'()*+,-./:;<=>?@\[\\\]^_`{|}~，。、；：？！…—·《》「」『』【】（）]*$')
                            if meaningless_pattern.match(forward_text):
                                forward_text = ''
                            
                            # 组合内容
                            if forward_text:
                                text = f"转发说明：{forward_text}\n\n被转发内容：{retweeted_text}"
                            else:
                                text = f"被转发内容：{retweeted_text}"
                        else:
                            # 原创微博直接处理正文
                            text = re.sub(r'<br\s*/?>', '\n', raw_text)
                            text = re.sub(r'<[^>]+>', '', text)
                            text = html.unescape(text)
                            text = text.strip() or "【无正文内容】"
                        
                        # 处理图片（优先用被转发微博的图片）
                        if retweeted_status:
                            pics = retweeted_status.get('pics', [])
                        else:
                            pics = mblog.get('pics', [])
                        pic_urls = [pic.get('large', {}).get('url', '') for pic in pics if pic.get('large')]
                        
                        # 处理视频（优先用被转发微博的视频，重点修复解析逻辑）
                        video_info = {
                            'play_page_url': '',  # 播放页链接
                            'cover_url': '',       # 封面链接
                        }
                        # 优先取转发微博的page_info
                        page_info = retweeted_status.get('page_info', {}) if retweeted_status else mblog.get('page_info', {})
                        
                        # 调试：打印page_info原始数据
                        sv.logger.debug(f"微博page_info数据: {page_info}")
                        
                        if page_info.get('type') in ['video', 'weibo_video']:  # 兼容视频类型
                            # 提取播放页链接（优先用fid，兼容object_id）
                            fid = page_info.get('fid') or page_info.get('object_id')
                            if fid:
                                video_info['play_page_url'] = f"https://video.weibo.com/show?fid={fid}"
                            
                            # 提取封面链接（多来源尝试）
                            # 1. 优先从page_pic获取
                            video_info['cover_url'] = page_info.get('page_pic', {}).get('url', '')
                            # 2. 从media_info的封面字段获取
                            if not video_info['cover_url']:
                                video_info['cover_url'] = page_info.get('media_info', {}).get('cover_image_url', '')
                            # 3. 从stream_url替换（备用方案）
                            if not video_info['cover_url']:
                                stream_url = page_info.get('media_info', {}).get('stream_url_hd', '')
                                if stream_url:
                                    # 简单替换后缀（根据实际情况调整，比如部分封面是独立字段）
                                    video_info['cover_url'] = stream_url.replace('.mp4', '.jpg').replace('.webm', '.jpg')
                        
                        # 处理视频链接显示（在推送部分）
                        # 处理视频（先显示封面，再显示链接）
                        if video_info['play_page_url']:
                            # 显示视频封面（如果有）
                            if video_info['cover_url']:
                                escaped_cover_url = escape(video_info['cover_url'])
                                text += f"\n[CQ:image,url={escaped_cover_url}]"
                            # 显示播放页链接
                            text += f"\n🎬 视频播放页：{video_info['play_page_url']}"
                        
                        # 应用时间格式化（转发微博用原微博时间）
                        if retweeted_status:
                            raw_time = retweeted_status.get('created_at', '未知时间')
                        else:
                            raw_time = mblog.get('created_at', '未知时间')
                        formatted_time = format_weibo_time(raw_time)
                        
                        # 统计数据（转发微博用原微博数据）
                        if retweeted_status:
                            reposts_count = retweeted_status.get('reposts_count', 0)
                            comments_count = retweeted_status.get('comments_count', 0)
                            attitudes_count = retweeted_status.get('attitudes_count', 0)
                        else:
                            reposts_count = mblog.get('reposts_count', 0)
                            comments_count = mblog.get('comments_count', 0)
                            attitudes_count = mblog.get('attitudes_count', 0)
                        
                        posts.append({
                            'id': mblog.get('id', ''),
                            'text': text,
                            'pics': pic_urls,
                            'video': video_info,
                            'created_at': formatted_time, 
                            'reposts_count': reposts_count,
                            'comments_count': comments_count,
                            'attitudes_count': attitudes_count
                        })
                        
                        if len(posts) >= count:
                            break
                
                return posts
    except Exception as e:
        sv.logger.error(f"获取微博内容失败: {e}")
        return []

# 检查并推送新微博
async def check_and_push_new_weibo():
    sv.logger.info("开始检查微博更新...")
    
    all_followed_uids = set()
    for group_id, follows in weibo_config['group_follows'].items():
        all_followed_uids.update(follows.keys())
    
    for uid in all_followed_uids:
        try:
            latest_posts = await get_weibo_user_latest_posts(uid)
            if not latest_posts:
                continue
                
            latest_posts.sort(key=lambda x: x['id'], reverse=True)
            latest_post = latest_posts[0]
            latest_post_id = latest_post['id']
            
            groups_to_push = []
            for group_id, follows in weibo_config['group_follows'].items():
                if uid in follows and weibo_config['group_enable'].get(group_id, True):
                    if latest_post_id > follows[uid]['last_post_id']:
                        groups_to_push.append(group_id)
                        weibo_config['group_follows'][group_id][uid]['last_post_id'] = latest_post_id
            
            if groups_to_push:
                save_config()
                user_info = await get_weibo_user_info(uid)
                # 修复：确保传递uid参数给推送函数
                if user_info:
                    await push_weibo_to_groups(groups_to_push, user_info['name'], uid, latest_post)
                else:
                    # 即使获取用户信息失败也尝试推送，使用已知的uid
                    await push_weibo_to_groups(groups_to_push, f'用户{uid}', uid, latest_post)
            
        except Exception as e:
            sv.logger.error(f"处理微博 {uid} 时出错: {e}")
            continue
    
    sv.logger.info("微博更新检查完成")

# 推送微博到指定群列表（包含视频封面显示）
async def push_weibo_to_groups(group_ids, name, uid, post):
    msg_parts = []
    
    # 使用传入的uid参数
    msg_parts.append(f"📢 {name} (ID: {uid}) 发布新微博：\n")
    msg_parts.append(f"{post['text']}\n\n")
    
    # 处理图片
    for pic_url in post['pics']:
        if pic_url:
            escaped_url = escape(pic_url)
            msg_parts.append(f"[CQ:image,url={escaped_url}]\n")
    
    # 统计信息与链接
    msg_parts.append(f"\n👍 {post['attitudes_count']}  🔁 {post['reposts_count']}  💬 {post['comments_count']}")
    msg_parts.append(f"\n发布时间：{post['created_at']}") 
    msg_parts.append(f"\n原文链接：https://m.weibo.cn/status/{post['id']}")
    msg_parts.append(f"\n取消关注请使用：取消关注微博 {uid}")
    full_message = ''.join(msg_parts)
    
    # 发送到目标群
    for group_id in group_ids:
        try:
            await sv.bot.send_group_msg(group_id=int(group_id), message=full_message)
            await asyncio.sleep(0.5)  # 避免发送过快
        except Exception as e:
            sv.logger.error(f"向群 {group_id} 推送失败: {e}，消息预览: {full_message[:200]}...")

# 定时任务：每1分钟检查一次
@sv.scheduled_job('interval', minutes=1)
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
    
    # 检查是否在黑名单中
    if uid in weibo_config['blacklist']:
        await bot.finish(ev, f'该微博ID({uid})已被禁止关注~')
    
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

# 修改全群关注微博功能，添加有效性检查和黑名单检查
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
    
    # 检查是否在黑名单中
    if uid in weibo_config['blacklist']:
        await bot.finish(ev, f'该微博ID({uid})已被禁止关注~')
    
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

# 新增：黑名单管理命令
@sv.on_prefix(('微博黑名单', '添加微博黑名单'))
async def add_blacklist(bot, ev: CQEvent):
    # 仅允许管理员操作
    if not priv.check_priv(ev, priv.ADMIN):
        await bot.finish(ev, '只有管理员才能操作黑名单哦~')
    
    uid = ev.message.extract_plain_text().strip()
    if not uid:
        await bot.finish(ev, '请输入要加入黑名单的微博ID哦~')
    
    if uid in weibo_config['blacklist']:
        await bot.finish(ev, f'微博ID({uid})已在黑名单中~')
    
    weibo_config['blacklist'].add(uid)
    save_config()
    await bot.send(ev, f'已成功将微博ID({uid})加入黑名单，禁止关注~')

@sv.on_prefix(('微博黑名单移除', '移除微博黑名单'))
async def remove_blacklist(bot, ev: CQEvent):
    # 仅允许管理员操作
    if not priv.check_priv(ev, priv.ADMIN):
        await bot.finish(ev, '只有管理员才能操作黑名单哦~')
    
    uid = ev.message.extract_plain_text().strip()
    if not uid:
        await bot.finish(ev, '请输入要移除黑名单的微博ID哦~')
    
    if uid not in weibo_config['blacklist']:
        await bot.finish(ev, f'微博ID({uid})不在黑名单中~')
    
    weibo_config['blacklist'].remove(uid)
    save_config()
    await bot.send(ev, f'已成功将微博ID({uid})从黑名单中移除~')

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
- 微博黑名单 [ID]：将指定微博ID加入黑名单（管理员）
- 微博黑名单移除 [ID]：将指定微博ID从黑名单移除（管理员）
- 查看微博黑名单：查看当前黑名单中的微博ID（管理员）
注：微博ID是指微博的数字ID，不是昵称哦~'''
    await bot.send(ev, help_msg)

# 查看微博黑名单
@sv.on_fullmatch(('查看微博黑名单',))
async def check_blacklist(bot, ev: CQEvent):
    if not priv.check_priv(ev, priv.ADMIN):
        await bot.finish(ev, '只有管理员才能查看黑名单哦~')
    
    if not weibo_config['blacklist']:
        await bot.send(ev, '当前黑名单为空~')
        return
    
    msg = "当前微博黑名单中的ID：\n"
    for uid in weibo_config['blacklist']:
        msg += f"- {uid}\n"
    await bot.send(ev, msg)

@on_startup
async def startup_check():
    sv.logger.info("微博推送插件已启动，正在进行首次微博检查...")
    await asyncio.sleep(10)  # 延迟检查，避免启动冲突
    await check_and_push_new_weibo()
