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

weibo_config = {
    'group_follows': {},      # {group_id: {weibo_id: {name: '微博名', last_post_id: '最后一条ID'}}}
    'group_enable': {},       # {group_id: True/False}
    'account_cache': {}       # {weibo_id: {name: '微博名', uid: '微博ID'}}
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
            weibo_config = json.load(f)
            for key in ['group_follows', 'group_enable', 'account_cache']:
                if key not in weibo_config:
                    weibo_config[key] = {}
    else:
        save_config()

def save_config():
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(weibo_config, f, ensure_ascii=False, indent=2)

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
    if uid in weibo_config['account_cache']:
        return weibo_config['account_cache'][uid]
    
    url = f'https://m.weibo.cn/api/container/getIndex?type=uid&value={uid}'
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, timeout=10) as resp:
                data = await resp.json()
                if data.get('ok') == 1:
                    user_info = data.get('data', {}).get('userInfo', {})
                    result = {
                        'name': user_info.get('screen_name', f'用户{uid}'),
                        'uid': uid
                    }
                    weibo_config['account_cache'][uid] = result
                    save_config()
                    return result
                sv.logger.warning(f"获取用户{uid}信息失败，API返回: {data}")
                return {'name': f'用户{uid}', 'uid': uid}
    except Exception as e:
        sv.logger.error(f"获取微博用户信息失败: {e}")
        return {'name': f'用户{uid}', 'uid': uid}

# 获取微博用户最新微博（包含视频封面提取）
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
                        raw_text = mblog.get('text', '')
                        
                        text = re.sub(r'<br\s*/?>', '\n', raw_text)  
                        text = re.sub(r'<[^>]+>', '', text) 
                        text = html.unescape(text) 
                        text = text.strip() or "【无正文内容】"  
                        
                        # 处理图片
                        pics = mblog.get('pics', [])
                        pic_urls = [pic.get('large', {}).get('url', '') for pic in pics if pic.get('large')]
                        
                        # 处理视频（包含封面）
                        video_info = {
                            'urls': [],
                            'cover_url': ''  # 新增视频封面字段
                        }
                        page_info = mblog.get('page_info', {})
                        if page_info.get('type') == 'video':
                            # 获取视频播放页链接
                            video_url = f"https://video.weibo.com/show?fid={page_info.get('fid', '')}"
                            video_info['urls'].append(video_url)
                            
                            # 提取视频封面
                            video_info['cover_url'] = page_info.get('page_pic', {}).get('url', '')
                            if not video_info['cover_url']:
                                # 备选方案：从其他字段获取封面
                                video_info['cover_url'] = page_info.get('media_info', {}).get('stream_url_hd', '').replace('mp4', 'jpg')
                            
                            # 尝试获取视频直接下载链接
                            if 'playback_list' in page_info:
                                for pl in page_info['playback_list']:
                                    if pl.get('url'):
                                        video_info['urls'].append(pl['url'])
                                        break
                        
                        # 应用时间格式化
                        formatted_time = format_weibo_time(mblog.get('created_at', '未知时间'))
                        
                        posts.append({
                            'id': mblog.get('id', ''),
                            'text': text,
                            'pics': pic_urls,
                            'video': video_info,  # 视频信息（包含封面和链接）
                            'created_at': formatted_time, 
                            'reposts_count': mblog.get('reposts_count', 0),
                            'comments_count': mblog.get('comments_count', 0),
                            'attitudes_count': mblog.get('attitudes_count', 0)
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
                await push_weibo_to_groups(groups_to_push, user_info['name'], latest_post)
            
        except Exception as e:
            sv.logger.error(f"处理微博 {uid} 时出错: {e}")
            continue
    
    sv.logger.info("微博更新检查完成")

# 推送微博到指定群列表（包含视频封面显示）
async def push_weibo_to_groups(group_ids, name, post):
    msg_parts = []
    
    msg_parts.append(f"📢 {name} 发布新微博：\n")
    
    msg_parts.append(f"{post['text']}\n\n")
    
    # 处理图片
    for i, pic_url in enumerate(post['pics']):
        if pic_url:
            escaped_url = escape(pic_url)
            # 如果是最后一张图且没有视频，不加换行
            if i == len(post['pics']) - 1 and not post['video']['urls']:
                msg_parts.append(f"[CQ:image,url={escaped_url}]")
            else:
                msg_parts.append(f"[CQ:image,url={escaped_url}]\n")
    
    # 处理视频（先显示封面，再显示链接）
    if post['video']['urls']:
        # 显示视频封面
        if post['video']['cover_url']:
            escaped_cover_url = escape(post['video']['cover_url'])
            msg_parts.append(f"[CQ:image,url={escaped_cover_url}]\n")
        
        # 显示视频链接
        msg_parts.append("🎬 视频链接：\n")
        for i, video_url in enumerate(post['video']['urls']):
            if video_url.startswith('http'):
                if i == 0:
                    msg_parts.append(f"[播放页] {video_url}\n")
                else:
                    msg_parts.append(f"[下载链接] {video_url}\n")
    
    # 统计信息与链接
    if post['text'] or post['pics'] or post['video']['urls']:
        msg_parts.append(f"\n👍 {post['attitudes_count']}  🔁 {post['reposts_count']}  💬 {post['comments_count']}")
        msg_parts.append(f"\n发布时间：{post['created_at']}") 
        msg_parts.append(f"\n原文链接：https://m.weibo.cn/status/{post['id']}")
    
    # 合并为完整消息
    full_message = ''.join(msg_parts)
    
    # 调试日志
    sv.logger.debug(f"推送消息内容: {full_message[:200]}...")
    
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
    
    if group_id not in weibo_config['group_follows']:
        weibo_config['group_follows'][group_id] = {}
    
    if uid in weibo_config['group_follows'][group_id]:
        name = weibo_config['group_follows'][group_id][uid]['name']
        await bot.finish(ev, f'本群已经关注过 {name} 啦~')
    
    user_info = await get_weibo_user_info(uid)
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

# 全群关注微博账号
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
    
    # 获取所有已加入的群
    groups = await bot.get_group_list()
    if not groups:
        await bot.finish(ev, '未加入任何群组，无法进行全群关注~')
    
    # 获取用户信息
    user_info = await get_weibo_user_info(uid)
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

# 取消关注微博账号
@sv.on_prefix(('取消关注微博', '取消订阅微博'))
async def unfollow_weibo(bot, ev: CQEvent):
    group_id = str(ev.group_id)
    user_id = ev.user_id
    
    if not flmt.check(user_id):
        await bot.finish(ev, f'操作太频繁啦，请{int(flmt.left_time(user_id)) + 1}秒后再试~')
    
    uid = ev.message.extract_plain_text().strip()
    if not uid:
        await bot.finish(ev, '请输入要取消关注的微博ID哦~')
    
    if group_id not in weibo_config['group_follows'] or uid not in weibo_config['group_follows'][group_id]:
        await bot.finish(ev, '本群没有关注这个微博账号哦~')
    
    name = weibo_config['group_follows'][group_id][uid]['name']
    del weibo_config['group_follows'][group_id][uid]
    if not weibo_config['group_follows'][group_id]:
        del weibo_config['group_follows'][group_id]
    
    save_config()
    flmt.start_cd(user_id)
    await bot.send(ev, f'本群已取消关注 {name} 的微博~')

# 查看本群关注的微博账号
@sv.on_fullmatch(('查看关注的微博', '我的关注微博', '微博关注列表'))
async def list_followed_weibo(bot, ev: CQEvent):
    group_id = str(ev.group_id)
    
    if group_id not in weibo_config['group_follows'] or not weibo_config['group_follows'][group_id]:
        await bot.finish(ev, '本群还没有关注任何微博账号哦~ 可以使用"关注微博 [ID]"来关注~')
    
    msg = ['本群关注的微博账号：\n']
    for i, (uid, info) in enumerate(weibo_config['group_follows'][group_id].items(), 1):
        msg.append(f"{i}. {info['name']} (ID: {uid})\n")
    
    msg.append('\n可以使用"取消关注微博 [ID]"来取消关注~')
    await bot.send(ev, ''.join(msg))

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
注：微博ID是指微博的数字ID，不是昵称哦~'''
    await bot.send(ev, help_msg)

@on_startup
async def startup_check():
    sv.logger.info("微博推送插件已启动，正在进行首次微博检查...")
    await asyncio.sleep(10)  # 延迟检查，避免启动冲突
    await check_and_push_new_weibo()
