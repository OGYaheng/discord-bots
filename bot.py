import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
import yt_dlp
import asyncio
import os
import pathlib
from collections import deque
import time
import concurrent.futures

# --------------------------
# 環境變數加載與初始化設定
# --------------------------
load_dotenv(dotenv_path="/home/container/bot.env")
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GUILD_ID = "GUILD_ID"

if not TOKEN:
    raise ValueError("❌ 未找到 DISCORD_BOT_TOKEN，請檢查 bot.env 文件配置！")

# --------------------------
# 機器人核心設定
# --------------------------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
music_queue = deque()
executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
processing_event = asyncio.Event()  # 使用事件代替鎖，提高並發效率
processing_event.set()  # 初始狀態為可用

# 用於追蹤歌曲開始時間和當前播放歌曲
song_start_times = {}  # 記錄歌曲開始播放的時間
now_playing_tracks = {}  # 記錄當前正在播放的歌曲
progress_messages = {}  # 用於保存顯示進度條的訊息，以便更新

# --------------------------
# 音訊處理設定 (fps.ms 優化)
# --------------------------
FFMPEG_OPTS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -fflags +discardcorrupt',
    'options': '-vn -loglevel error -preset ultrafast'
}

def get_cookies_config():
    """動態獲取 cookies 設定"""
    cookies_path = "/home/container/cookies.txt"
    if pathlib.Path(cookies_path).exists():
        return {"cookiefile": cookies_path}
    return {}

YDL_OPTS = {
    **get_cookies_config(),
    'format': 'worstaudio',  # 使用最低音質
    'noplaylist': True,      #歌單
    'quiet': True,
    'no_warnings': True,
    'default_search': 'ytsearch',
    'extract_flat': True,  # 只提取基本資訊
    'skip_download': True,
    'cachedir': '/tmp',  # 使用臨時目錄當快取
    'socket_timeout': 3,  # 降低超時時間以加快回應 
    'retries': 1,
    'nocheckcertificate': True,
    'geo_bypass': True,
    'source_address': '0.0.0.0',
}

# --------------------------
# 進度條功能
# --------------------------
def create_progress_bar(current_time, total_time, bar_size=15, filled_char="▓", empty_char="░"):
    """創建歌曲進度條"""
    if total_time <= 0:
        progress = 0
    else:
        progress = min(current_time / total_time, 1.0)  # 確保不超過100%
    
    filled_length = round(bar_size * progress)
    empty_length = bar_size - filled_length
    
    bar = filled_char * filled_length + empty_char * empty_length
    
    # 格式化時間為分:秒
    current_minutes = int(current_time // 60)
    current_seconds = int(current_time % 60)
    total_minutes = int(total_time // 60)
    total_seconds = int(total_time % 60)
    
    time_display = f"{current_minutes:02d}:{current_seconds:02d}/{total_minutes:02d}:{total_seconds:02d}"
    
    return f"{bar} {time_display}"

# --------------------------
# 自動更新進度條任務
# --------------------------
async def update_progress_bar(guild_id, channel_id, message_id):
    """定期更新進度條的任務"""
    try:
        channel = bot.get_channel(channel_id)
        if not channel:
            return
        
        message = await channel.fetch_message(message_id)
        if not message:
            return
        
        # 最多更新20次，每5秒一次，共100秒
        for _ in range(20):
            # 檢查歌曲是否還在播放
            if guild_id not in now_playing_tracks or guild_id not in song_start_times:
                break
                
            guild = bot.get_guild(guild_id)
            if not guild or not guild.voice_client or not guild.voice_client.is_playing():
                break
            
            # 獲取當前歌曲並計算進度
            track = now_playing_tracks[guild_id]
            current_time = time.time() - song_start_times[guild_id]
            total_time = track['duration']
            
            # 更新進度條
            progress_bar = create_progress_bar(current_time, total_time)
            
            embed = message.embeds[0]
            
            # 找到並更新進度欄位
            for i, field in enumerate(embed.fields):
                if field.name == "進度":
                    embed.set_field_at(i, name="進度", value=f"`{progress_bar}`", inline=False)
                    break
            else:
                # 如果找不到進度欄位，添加一個
                embed.add_field(name="進度", value=f"`{progress_bar}`", inline=False)
            
            await message.edit(embed=embed)
            
            # 等待5秒
            await asyncio.sleep(5)
            
    except Exception as e:
        print(f"更新進度條時出錯: {e}")

# --------------------------
# 自動完成功能
# --------------------------
async def song_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    """提供熱門歌曲的自動完成建議"""
    songs = ["Despacito", "Shape of You", "Uptown Funk", "See You Again", "Sugar", "Happy", "PPAP"]
    return [
        app_commands.Choice(name=song, value=song)
        for song in songs if current.lower() in song.lower()
    ]

# --------------------------
# 優化的 YouTube 資訊提取
# --------------------------
async def extract_song_info(query: str):
    """以非同步方式提取歌曲資訊 (優化3.0)"""
    start_time = time.monotonic()
    
    # 預處理查詢
    if not query.startswith(('http://', 'https://')):
        query = f"ytsearch:{query}"
    
    # 使用線程池執行耗時操作
    try:
        loop = asyncio.get_running_loop()
        
        # 實際的提取函數
        def _extract():
            with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
                info = ydl.extract_info(query, download=False)
                return info
        
        # 非同步執行
        info = await loop.run_in_executor(executor, _extract)
        
        # 處理搜索結果
        if 'entries' in info:
            if not info['entries']:
                raise ValueError("找不到相關歌曲")
            info = info['entries'][0]
        
        # 構建簡化的曲目信息
        track = {
            'url': info['url'],
            'title': info.get('title', '未知曲目'),
            'thumbnail': info.get('thumbnail'),
            'duration': info.get('duration', 0),
            'webpage_url': info.get('webpage_url', '')
        }
        
        duration = (time.monotonic() - start_time) * 1000
        print(f"提取歌曲資訊耗時: {duration:.2f}ms")
        return track
        
    except Exception as e:
        print(f"提取歌曲資訊失敗: {e}")
        raise ValueError(f"無法獲取音樂資訊: {str(e)}")

# --------------------------
# 音樂播放核心 (優化8.0)
# --------------------------
async def play_next(vc, interaction=None):
    """播放下一首歌曲 (優化版)"""
    if not vc or not vc.is_connected():
        return
    
    if not music_queue:
        return
    
    if not processing_event.is_set():
        await processing_event.wait()
    
    processing_event.clear()  # 鎖定，防止其他操作
    
    try:
        if not music_queue:
            processing_event.set()
            return
            
        track = music_queue.popleft()
        guild_id = vc.guild.id
        
        # 記錄歌曲開始時間和當前播放歌曲
        song_start_times[guild_id] = time.time()
        now_playing_tracks[guild_id] = track
        
        def after_play(error):
            if error:
                print(f"播放錯誤: {error}")
            
            # 使用事件而非直接調用，避免遞迴問題
            bot.loop.call_soon_threadsafe(lambda: asyncio.create_task(play_next_wrapper(vc)))
        
        try:
            # 直接使用 URL 串流
            source = discord.FFmpegPCMAudio(track['url'], **FFMPEG_OPTS)
            
            # 調低音量以避免失真
            source = discord.PCMVolumeTransformer(source, volume=0.5)
            
            # 開始播放
            vc.play(source, after=after_play)
            print(f"開始播放: {track['title']}")
            
            # 創建帶有進度條的播放提示
            current_time = 0
            total_time = track['duration']
            progress_bar = create_progress_bar(current_time, total_time)
            
            embed = discord.Embed(title="🎶 正在播放", color=0x00ff00)
            embed.add_field(name="曲目", value=track['title'], inline=False)
            embed.add_field(name="進度", value=f"`{progress_bar}`", inline=False)
            
            # 添加時長資訊
            if track['duration'] > 0:
                minutes = track['duration'] // 60
                seconds = track['duration'] % 60
                embed.add_field(name="時長", value=f"{minutes}:{seconds:02d}", inline=True)
                
            if track.get('thumbnail'):
                embed.set_thumbnail(url=track['thumbnail'])
            
            # 發送播放通知並保存消息以便更新進度條
            if interaction and not interaction.is_expired():
                message = await interaction.followup.send(embed=embed)
                # 啟動進度條更新任務
                if guild_id in progress_messages:
                    # 清除舊的進度條消息
                    try:
                        old_channel_id, old_message_id = progress_messages[guild_id]
                        old_channel = bot.get_channel(old_channel_id)
                        if old_channel:
                            try:
                                old_message = await old_channel.fetch_message(old_message_id)
                                await old_message.delete()
                            except:
                                pass
                    except:
                        pass
                
                # 保存新的進度條消息
                progress_messages[guild_id] = (interaction.channel_id, message.id)
                asyncio.create_task(update_progress_bar(guild_id, interaction.channel_id, message.id))
            elif vc.channel:
                message = await vc.channel.send(embed=embed)
                # 保存消息以便更新進度條
                progress_messages[guild_id] = (vc.channel.id, message.id)
                asyncio.create_task(update_progress_bar(guild_id, vc.channel.id, message.id))
                
        except Exception as e:
            print(f"播放錯誤: {e}")
            if vc.channel:
                asyncio.create_task(vc.channel.send(f"❌ 播放失敗: {str(e)}"))
            
            # 釋放事件
            processing_event.set()
            
            # 嘗試播放下一首
            await play_next_wrapper(vc)
    finally:
        # 確保事件被釋放
        if not processing_event.is_set():
            processing_event.set()

async def play_next_wrapper(vc):
    """包裝函數，確保播放下一首的安全調用"""
    await asyncio.sleep(0.5)  # 增加延遲
    processing_event.set()  # 確保事件被釋放
    await play_next(vc)

# --------------------------
# 斜線指令定義
# --------------------------
@bot.tree.command(name="join", description="加入語音頻道")
async def join(interaction: discord.Interaction):
    """加入使用者所在的語音頻道"""
    await interaction.response.defer()
    try:
        if not interaction.user.voice:
            return await interaction.followup.send("❌ 請先加入語音頻道！")

        vc = interaction.guild.voice_client
        if vc and vc.is_connected():
            return await interaction.followup.send("⚠️ 機器人已在此伺服器的語音頻道中！")

        await interaction.user.voice.channel.connect()
        await interaction.followup.send(f"✅ 已加入 {interaction.user.voice.channel.name}")
    except Exception as e:
        await interaction.followup.send(f"⚠️ 連線失敗: {str(e)}")

@bot.tree.command(name="play", description="播放或加入音樂到隊列")
@app_commands.describe(query="YouTube 連結或搜尋關鍵字")
@app_commands.autocomplete(query=song_autocomplete)
async def play(interaction: discord.Interaction, query: str):
    """播放或加入音樂到隊列"""
    start_time = time.monotonic()
    await interaction.response.defer(thinking=True)
    
    try:
        vc = interaction.guild.voice_client
        if not vc or not vc.is_connected():
            return await interaction.followup.send("❌ 請先使用 `/join` 指令！")

        processing_msg = await interaction.followup.send("🔍 正在處理您的請求...")

        # 優化的音樂解析流程
        try:
            track = await extract_song_info(query)
        except ValueError as e:
            await processing_msg.delete()
            await interaction.followup.send(f"❌ {str(e)}")
            return

        # 檢查是否已在播放並將歌曲加入隊列
        is_playing = vc.is_playing()
        
        # 等待事件釋放
        await processing_event.wait()
        processing_event.clear()
        
        try:
            music_queue.append(track)
        finally:
            processing_event.set()
        
        await processing_msg.delete()
        
        # 根據播放狀態選擇回應方式
        if is_playing:
            embed = discord.Embed(title="🎵 已加入隊列", color=0x00ff00)
            embed.add_field(name="曲目", value=track['title'], inline=False)
            
            if track['duration'] > 0:
                minutes = track['duration'] // 60
                seconds = track['duration'] % 60
                embed.add_field(name="時長", value=f"{minutes}:{seconds:02d}", inline=True)
                
            if track.get('thumbnail'):
                embed.set_thumbnail(url=track['thumbnail'])
                
            await interaction.followup.send(embed=embed)
        else:
            # 如果沒有正在播放的歌曲，立即開始播放
            await play_next(vc, interaction)
            
        # 記錄播放效能
        duration = (time.monotonic() - start_time) * 1000
        print(f"play 執行時間: {duration:.2f}ms")
        
    except Exception as e:
        await interaction.followup.send(f"❌ 發生錯誤: {str(e)}")
        print(f"播放錯誤: {e}")

@bot.tree.command(name="skip", description="跳過目前播放的歌曲")
async def skip(interaction: discord.Interaction):
    """跳過目前播放的歌曲"""
    await interaction.response.defer()
    vc = interaction.guild.voice_client
    if not vc or not vc.is_playing():
        return await interaction.followup.send("❌ 目前沒有正在播放的歌曲！")
    
    vc.stop()  # 停止當前播放，after_play 回調會自動播放下一首
    await interaction.followup.send("⏭️ 已跳過當前歌曲")

@bot.tree.command(name="playlist", description="查看目前的音樂隊列")
async def playlist(interaction: discord.Interaction):
    """顯示目前的音樂隊列"""
    if not music_queue:
        return await interaction.response.send_message("🎵 隊列中沒有任何歌曲！")
    
    embed = discord.Embed(title="🎶 音樂隊列", color=0x00ff00)
    
    # 等待事件鎖釋放
    await processing_event.wait()
    
    for idx, track in enumerate(music_queue, start=1):
        duration_str = "未知時長"
        if track['duration'] > 0:
            minutes = track['duration'] // 60
            seconds = track['duration'] % 60
            duration_str = f"{minutes}:{seconds:02d}"
            
        embed.add_field(
            name=f"{idx}. {track['title']}", 
            value=f"時長: {duration_str}", 
            inline=False
        )
    
    await interaction.response.send_message(embed=embed)

# --------------------------
# 網路延遲檢查
# --------------------------
@bot.tree.command(name="ping", description="檢查機器人延遲")
async def ping_slash(interaction: discord.Interaction):
    """檢查機器人延遲"""
    start_time = time.monotonic()
    await interaction.response.defer()
    end_time = time.monotonic()
    
    latency_ms = (end_time - start_time) * 1000
    api_latency_ms = bot.latency * 1000
    
    embed = discord.Embed(title="🏓 延遲資訊", color=0x00ff00)
    embed.add_field(name="延遲", value=f"{latency_ms:.2f}ms", inline=True)
    embed.add_field(name="API 延遲", value=f"{api_latency_ms:.2f}ms", inline=True)
    
    await interaction.followup.send(embed=embed)

# --------------------------
# 指令同步與錯誤處理
# --------------------------
@bot.event
async def on_ready():
    print(f"✅ 機器人已上線：{bot.user}")
    
    try:
        # 先清除全域指令
        await bot.tree.sync()
        print("✅ 已清除全域指令")
        
        # 專用於伺服器的指令同步
        guild = discord.Object(id=GUILD_ID)
        bot.tree.clear_commands(guild=guild)
        
        # 手動添加命令 (確認指令存在)
        for command in bot.tree.get_commands():
            print(f"正在添加指令：{command.name}")
            bot.tree.add_command(command, guild=guild)
        
        # 同步到伺服器
        synced = await bot.tree.sync(guild=guild)
        print(f"✅ 已同步 {len(synced)} 個指令至伺服器 {GUILD_ID}")
    except Exception as e:
        print(f"❌ 指令同步錯誤：{e}")
        
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    error_msg = f"❌ 指令錯誤: {str(error)}"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(error_msg)
        else:
            await interaction.response.send_message(error_msg)
    except:
        await interaction.channel.send(error_msg)
        
# --------------------------
# 手動控制指令
# --------------------------
@bot.command()
async def sync(ctx):
    """手動同步指令 (管理員專用)"""
    try:
        guild = discord.Object(id=GUILD_ID)
        
        # 檢查已註冊的指令
        commands = await bot.tree.fetch_commands(guild=guild)
        print(f"已發現 {len(commands)} 個已註冊指令")
        
        # 重新同步所有指令
        bot.tree.clear_commands(guild=guild)
        
        # 手動添加所有斜線指令
        for cmd in [join, play, skip, playlist, ping_slash, clean]:
            bot.tree.add_command(cmd, guild=guild)
        
        # 同步指令
        fmt = await bot.tree.sync(guild=guild)
        
        await ctx.send(f"✅ 已同步 {len(fmt)} 個指令")
    except Exception as e:
        await ctx.send(f"❌ 同步失敗: {e}")

@bot.command()
async def fixplay(ctx):
    """強制修復 play 指令"""
    try:
        guild = discord.Object(id=GUILD_ID)
        bot.tree.remove_command("play", guild=guild)
        bot.tree.add_command(play, guild=guild)
        await bot.tree.sync(guild=guild)
        await ctx.send("✅ play 指令已修復")
    except Exception as e:
        await ctx.send(f"❌ 修復失敗: {e}")

# --------------------------
# 測試和維護指令
# --------------------------
@bot.command(name="ping")
async def ping(ctx):
    """檢查機器人延遲"""
    start_time = time.monotonic()
    message = await ctx.send("測量延遲中...")
    end_time = time.monotonic()
    
    latency_ms = (end_time - start_time) * 1000
    api_latency_ms = bot.latency * 1000
    
    await message.edit(content=f"🏓 延遲: {latency_ms:.2f}ms\nAPI 延遲: {api_latency_ms:.2f}ms")

@bot.command(name="clean")
async def clean_queue(ctx):
    """清空音樂隊列"""
    # 等待事件釋放
    await processing_event.wait()
    processing_event.clear()
    
    try:
        queue_length = len(music_queue)
        music_queue.clear()
        await ctx.send(f"✅ 已清空 {queue_length} 首歌曲")
    finally:
        processing_event.set()

# --------------------------
# 啟動機器人
# --------------------------
if __name__ == "__main__":
    bot.run(TOKEN)
