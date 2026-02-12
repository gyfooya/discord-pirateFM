import discord
from discord import FFmpegPCMAudio, PCMVolumeTransformer
from discord.ext import commands
import yaml
import aiohttp
import asyncio
import logging
import yt_dlp
import json
import os
from collections import deque
import re

# Set up logging
logging.basicConfig(level=logging.INFO)

# Load configuration
with open('config.yaml', 'r') as f:
    config = yaml.safe_load(f)

# Validate configuration
required_keys = ["icecast_url", "guild_id", "voice_channel_id", "discord_bot_key"]
for key in required_keys:
    if key not in config:
        logging.error(f"Missing required config key: {key}")
        exit(1)

# Extract the base URL from the icecast_url and construct the status URL
icecast_base_url = '/'.join(config["icecast_url"].split('/')[:-1])
icecast_status_url = f"{icecast_base_url}/status-json.xsl"

# YouTube-DL options - Fixed for proper streaming
ytdl_format_options = {
    'format': 'bestaudio/best',
    'extractaudio': True,
    'audioformat': 'mp3',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
}

# Fixed FFmpeg options for better compatibility
ffmpeg_options = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn -f s16le -ar 48000 -ac 2',
}

ytdl = yt_dlp.YoutubeDL(ytdl_format_options)

class YTDLSource(PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume=volume)
        self.data = data
        self.title = data.get('title')
        self.url = data.get('url')
        self.duration = data.get('duration')
        self.uploader = data.get('uploader')

    @classmethod
    async def create_source(cls, search: str, *, loop=None, volume=0.5):
        loop = loop or asyncio.get_event_loop()
        
        try:
            # Extract info from YouTube
            data = await loop.run_in_executor(None, lambda: ytdl.extract_info(search, download=False))
            
            if 'entries' in data:
                # Take first item from a playlist
                data = data['entries'][0]
            
            # Get the direct URL for streaming
            url = data['url']
            source = discord.FFmpegPCMAudio(url, **ffmpeg_options)
            return cls(source, data=data, volume=volume)
        except Exception as e:
            logging.error(f"Error creating YouTube source: {e}")
            return None

class MusicQueue:
    def __init__(self):
        self.queue = deque()
        self.current_track = None
        self.repeat_mode = False
        self.shuffle_mode = False

    def add_track(self, track_info):
        self.queue.append(track_info)

    def add_playlist(self, playlist_tracks):
        self.queue.extend(playlist_tracks)

    def get_next_track(self):
        if self.repeat_mode and self.current_track:
            return self.current_track
        
        if self.queue:
            self.current_track = self.queue.popleft()
            return self.current_track
        return None

    def skip_track(self):
        if self.queue:
            self.current_track = self.queue.popleft()
            return self.current_track
        return None

    def clear(self):
        self.queue.clear()
        self.current_track = None

    def get_queue_info(self):
        return list(self.queue)

    def is_empty(self):
        return len(self.queue) == 0 and self.current_track is None

# Initialize bot
intents = discord.Intents.default()
intents.guilds = True
intents.voice_states = True
intents.message_content = True
intents.members = True  # Add members intent to get Member objects

bot = commands.Bot(command_prefix='!', intents=intents)

# Global variables
music_queue = MusicQueue()
default_volume = 0.5
current_source = None
is_playing_stream = False
auto_join_enabled = True

# Helper function to safely get member voice state
def get_member_voice(ctx):
    """Safely get a member's voice state"""
    if not ctx.guild:
        return None, "This command can only be used in a server!"
    
    # Try multiple ways to get the member object
    member = None
    
    # Method 1: If ctx.author is already a Member
    if hasattr(ctx.author, 'voice'):
        member = ctx.author
    
    # Method 2: Get member from guild
    if not member:
        member = ctx.guild.get_member(ctx.author.id)
    
    # Method 3: Fetch member if needed
    if not member:
        try:
            # This is synchronous but should work
            for m in ctx.guild.members:
                if m.id == ctx.author.id:
                    member = m
                    break
        except:
            pass
    
    if not member:
        return None, "Could not find you in this server! Try rejoining the server."
    
    if not hasattr(member, 'voice') or not member.voice or not member.voice.channel:
        return None, "You need to be in a voice channel to use this command!"
    
    return member.voice.channel, None

# Utility functions
async def fetch_icecast_status(url):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data
                else:
                    logging.error(f"Failed to fetch data: {resp.status}")
                    return None
    except Exception as e:
        logging.error(f"Error fetching Icecast status: {e}")
        return None

async def get_now_playing():
    status_data = await fetch_icecast_status(icecast_status_url)

    if status_data and "icestats" in status_data:
        icestats = status_data["icestats"]

        artist = icestats.get('artist')
        title = icestats.get('title')
        if artist and title:
            now_playing = f"{artist} : {title}"
        elif 'server_name' in icestats:
            now_playing = icestats['server_name']
        else:
            host = icestats.get('host', 'unknown host')
            stream_url = config["icecast_url"]
            source = stream_url.split('/')[-1]
            now_playing = f"{host} : {source}"

        return now_playing
    else:
        return "Unknown"

async def update_status_task():
    global is_playing_stream, music_queue
    current_status = None
    while True:
        try:
            if is_playing_stream:
                now_playing = await get_now_playing()
                status_text = f"🎵 {now_playing}"
            elif music_queue.current_track:
                track = music_queue.current_track
                status_text = f"🎵 {track['title']}"
            else:
                status_text = "Ready for music!"
            
            if status_text != current_status:
                await bot.change_presence(activity=discord.Game(name=status_text))
                current_status = status_text
                
            await asyncio.sleep(30)
        except Exception as e:
            logging.error(f"Error updating status: {e}")
            await asyncio.sleep(60)

async def play_next(ctx):
    global music_queue, default_volume
    next_track = music_queue.get_next_track()
    
    if next_track:
        if next_track['type'] == 'youtube':
            # Create source for this track if it doesn't exist
            if not next_track.get('source'):
                source = await YTDLSource.create_source(next_track['url'], volume=default_volume)
                next_track['source'] = source
            else:
                source = next_track['source']
            
            if source:
                # Update current track in the queue
                music_queue.current_track = next_track
                
                def after_playing(error):
                    if error:
                        logging.error(f'Player error: {error}')
                    asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)
                
                ctx.voice_client.play(source, after=after_playing)
                await ctx.send(f"🎵 Now playing: **{next_track['title']}**")
            else:
                await ctx.send(f"❌ Failed to play: **{next_track['title']}**, skipping...")
                await play_next(ctx)
        elif next_track['type'] == 'icecast':
            await play_icecast_stream(ctx, next_track['url'])
    else:
        # Queue is empty, clear current track
        music_queue.current_track = None
        await ctx.send("📭 Queue finished! Use `!play <song>` to add more music.")

async def handle_playlist(ctx, playlist_url):
    global music_queue
    try:
        # Extract playlist info
        data = await bot.loop.run_in_executor(None, lambda: ytdl.extract_info(playlist_url, download=False))
        
        if 'entries' in data:
            playlist_title = data.get('title', 'Unknown Playlist')
            entries = data['entries'][:50]  # Limit to 50 tracks
            
            await ctx.send(f"Adding playlist: **{playlist_title}** ({len(entries)} tracks)")
            
            for entry in entries:
                if entry:
                    track_info = {
                        'type': 'youtube',
                        'title': entry.get('title', 'Unknown'),
                        'url': entry.get('webpage_url', ''),
                        'duration': entry.get('duration'),
                        'uploader': entry.get('uploader', 'Unknown'),
                        'source': None
                    }
                    music_queue.add_track(track_info)
            
            # Start playing if nothing is currently playing
            if not ctx.voice_client.is_playing():
                await play_next(ctx)
                
    except Exception as e:
        await ctx.send(f"Error loading playlist: {str(e)}")

async def play_icecast_stream(ctx, stream_url):
    global default_volume, is_playing_stream, current_source
    try:
        await ctx.send(f"🔄 Connecting to stream: **{stream_url}**")
        
        # Test the stream URL first
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(stream_url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status != 200:
                        await ctx.send(f"❌ Stream not accessible. Status: {response.status}")
                        return
            except asyncio.TimeoutError:
                await ctx.send("❌ Stream connection timed out. The stream might be offline.")
                return
            except Exception as e:
                await ctx.send(f"❌ Stream test failed: {str(e)}")
                return
        
        # Create the audio source with better error handling
        try:
            audio_source = FFmpegPCMAudio(
                stream_url, 
                before_options='-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
                options='-vn -loglevel error'
            )
            audio_source = PCMVolumeTransformer(audio_source, volume=default_volume)
        except Exception as e:
            await ctx.send(f"❌ Failed to create audio source: {str(e)}")
            logging.error(f"Audio source creation error: {e}")
            return
        
        # Play the stream
        try:
            ctx.voice_client.play(audio_source, after=lambda e: logging.error(f'Stream error: {e}') if e else None)
            is_playing_stream = True
            current_source = audio_source
            
            await ctx.send(f"✅ Now streaming from: **{stream_url}**")
        except Exception as e:
            await ctx.send(f"❌ Failed to play stream: {str(e)}")
            logging.error(f"Stream playback error: {e}")
            return
            
    except Exception as e:
        await ctx.send(f"❌ Stream error: {str(e)}")
        logging.error(f"General stream error: {e}")

# Music control commands
@bot.command(name='play', help='Play a YouTube video or add to queue')
async def play(ctx, *, search):
    global music_queue, default_volume, is_playing_stream
    
    # Use helper function to safely get voice channel
    voice_channel, error = get_member_voice(ctx)
    if error:
        await ctx.send(error)
        return
    
    # Connect to voice channel if not already connected
    if not ctx.voice_client:
        await voice_channel.connect()
    elif ctx.voice_client.channel != voice_channel:
        await ctx.voice_client.move_to(voice_channel)

    # Stop stream if currently playing
    if is_playing_stream and ctx.voice_client.is_playing():
        ctx.voice_client.stop()
        is_playing_stream = False
        await ctx.send("🔄 Switched from stream to music queue")

    async with ctx.typing():
        # Check if it's a playlist URL
        if 'playlist' in search or 'list=' in search:
            await handle_playlist(ctx, search)
        else:
            # Single video
            source = await YTDLSource.create_source(search, volume=default_volume)
            if source:
                track_info = {
                    'type': 'youtube',
                    'title': source.title,
                    'url': search,
                    'duration': source.duration,
                    'uploader': source.uploader,
                    'source': source
                }
                
                if ctx.voice_client.is_playing():
                    music_queue.add_track(track_info)
                    position = len(music_queue.get_queue_info())
                    await ctx.send(f"✅ Added to queue (position {position}): **{source.title}**")
                else:
                    music_queue.current_track = track_info
                    
                    def after_playing(error):
                        if error:
                            logging.error(f'Player error: {error}')
                        asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)
                    
                    ctx.voice_client.play(source, after=after_playing)
                    await ctx.send(f"🎵 Now playing: **{source.title}**")
            else:
                await ctx.send("❌ Could not find or play that video!")

@bot.command(name='stream', help='Play an Icecast stream')
async def stream(ctx, url=None):
    global is_playing_stream
    
    # Use helper function to safely get voice channel
    voice_channel, error = get_member_voice(ctx)
    if error:
        await ctx.send(error)
        return
    
    # Connect to voice channel if not already connected
    if not ctx.voice_client:
        try:
            await voice_channel.connect()
            await ctx.send(f"Connected to **{voice_channel.name}**")
        except Exception as e:
            await ctx.send(f"Failed to connect to voice channel: {str(e)}")
            return
    elif ctx.voice_client.channel != voice_channel:
        try:
            await ctx.voice_client.move_to(voice_channel)
            await ctx.send(f"Moved to **{voice_channel.name}**")
        except Exception as e:
            await ctx.send(f"Failed to move to voice channel: {str(e)}")
            return

    stream_url = url or config["icecast_url"]
    
    # Stop current playback
    if ctx.voice_client.is_playing():
        ctx.voice_client.stop()
        await asyncio.sleep(0.5)  # Give it time to stop
    
    # Clear queue when switching to stream
    if not is_playing_stream:
        music_queue.clear()
    
    await play_icecast_stream(ctx, stream_url)

@bot.command(name='volume', help='Change the volume (0-100)')
async def set_volume(ctx, vol: int):
    global default_volume
    
    if not ctx.voice_client:
        await ctx.send("Not connected to a voice channel!")
        return

    if not 0 <= vol <= 100:
        await ctx.send("Volume must be between 0 and 100!")
        return

    # Update default volume for future tracks
    default_volume = vol / 100
    
    # Update current playing source volume if it exists and supports volume control
    if ctx.voice_client.source:
        if hasattr(ctx.voice_client.source, 'volume'):
            ctx.voice_client.source.volume = default_volume
        elif music_queue.current_track and music_queue.current_track.get('source'):
            # Update the stored source volume
            source = music_queue.current_track['source']
            if hasattr(source, 'volume'):
                source.volume = default_volume
    
    await ctx.send(f"Volume set to {vol}%")

@bot.command(name='pause', help='Pause the current track')
async def pause(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.pause()
        await ctx.send("Paused ⏸️")
    else:
        await ctx.send("Nothing is currently playing!")

@bot.command(name='resume', help='Resume the current track')
async def resume(ctx):
    if ctx.voice_client and ctx.voice_client.is_paused():
        ctx.voice_client.resume()
        await ctx.send("Resumed ▶️")
    else:
        await ctx.send("Nothing is currently paused!")

@bot.command(name='skip', help='Skip the current track')
async def skip(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()
        await ctx.send("Skipped ⏭️")
    else:
        await ctx.send("Nothing is currently playing!")

@bot.command(name='queue', help='Show the current queue')
async def show_queue(ctx):
    try:
        global music_queue, is_playing_stream
        
        # Debug info
        logging.info(f"Queue command called - is_playing_stream: {is_playing_stream}")
        logging.info(f"Current track: {music_queue.current_track}")
        logging.info(f"Queue size: {len(music_queue.get_queue_info())}")
        
        # Simple check for voice client
        if not ctx.voice_client:
            await ctx.send("❌ Not connected to any voice channel!")
            return
        
        # Check if streaming
        if is_playing_stream:
            await ctx.send("🔴 **Currently Streaming**\nIcecast stream is playing. No queue while streaming.")
            return
        
        # Check if we have a current track
        current = music_queue.current_track
        queue_list = music_queue.get_queue_info()
        
        if not current and not queue_list:
            await ctx.send("📭 **Queue is empty!**\nUse `!play <song>` to add music.")
            return
        
        # Build simple queue message
        message = "🎵 **Music Queue**\n\n"
        
        if current:
            message += f"**Now Playing:**\n🎵 {current['title']}\n\n"
        
        if queue_list:
            message += f"**Up Next ({len(queue_list)} tracks):**\n"
            for i, track in enumerate(queue_list[:10], 1):
                message += f"`{i}.` {track['title']}\n"
            
            if len(queue_list) > 10:
                message += f"... and {len(queue_list) - 10} more tracks\n"
        
        await ctx.send(message)
        
    except Exception as e:
        logging.error(f"Queue command error: {e}")
        await ctx.send(f"❌ Error showing queue: {str(e)}")

@bot.command(name='clear', help='Clear the music queue')
async def clear_queue(ctx):
    global music_queue
    music_queue.clear()
    await ctx.send("Queue cleared! 🗑️")

@bot.command(name='stop', help='Stop playing and disconnect')
async def stop(ctx):
    global music_queue, is_playing_stream
    
    if ctx.voice_client:
        ctx.voice_client.stop()
        await ctx.voice_client.disconnect()
        music_queue.clear()
        is_playing_stream = False
        await ctx.send("Stopped and disconnected! 👋")
    else:
        await ctx.send("Not connected to a voice channel!")

@bot.command(name='nowplaying', aliases=['np'], help='Show current track info')
async def now_playing_cmd(ctx):
    try:
        global is_playing_stream, music_queue
        
        # Debug logging
        logging.info(f"Now playing command called")
        logging.info(f"Voice client exists: {ctx.voice_client is not None}")
        if ctx.voice_client:
            logging.info(f"Is playing: {ctx.voice_client.is_playing()}")
        logging.info(f"Is streaming: {is_playing_stream}")
        logging.info(f"Current track: {music_queue.current_track}")
        
        if not ctx.voice_client:
            await ctx.send("❌ Not connected to any voice channel!")
            return
        
        if not ctx.voice_client.is_playing():
            await ctx.send("⏸️ Nothing is currently playing!")
            return
        
        if is_playing_stream:
            await ctx.send("🔴 **Now Streaming**\nIcecast stream is currently playing.")
            return
        
        if music_queue.current_track:
            track = music_queue.current_track
            title = track.get('title', 'Unknown Track')
            uploader = track.get('uploader', 'Unknown Artist')
            
            message = f"🎵 **Now Playing**\n\n"
            message += f"**Title:** {title}\n"
            message += f"**Artist:** {uploader}\n"
            
            if track.get('duration'):
                mins, secs = divmod(track['duration'], 60)
                message += f"**Duration:** {mins}:{secs:02d}\n"
            
            queue_size = len(music_queue.get_queue_info())
            if queue_size > 0:
                message += f"\n📋 **{queue_size}** track(s) in queue"
            
            await ctx.send(message)
        else:
            await ctx.send("🤔 Something is playing but I can't identify what it is!")
            
    except Exception as e:
        logging.error(f"Now playing command error: {e}")
        await ctx.send(f"❌ Error getting current track info: {str(e)}")

@bot.command(name='debug', help='Show debug information')
async def debug_info(ctx):
    try:
        global music_queue, is_playing_stream, current_source
        
        message = "🔧 **Debug Information**\n\n"
        
        # Voice client info
        if ctx.voice_client:
            message += f"✅ **Voice Client:** Connected to {ctx.voice_client.channel.name}\n"
            message += f"🎵 **Is Playing:** {ctx.voice_client.is_playing()}\n"
            message += f"⏸️ **Is Paused:** {ctx.voice_client.is_paused()}\n"
        else:
            message += "❌ **Voice Client:** Not connected\n"
        
        # Stream info
        message += f"📡 **Is Streaming:** {is_playing_stream}\n"
        
        # Queue info  
        message += f"📋 **Current Track:** {music_queue.current_track is not None}\n"
        if music_queue.current_track:
            message += f"   - Title: {music_queue.current_track.get('title', 'N/A')}\n"
        
        queue_list = music_queue.get_queue_info()
        message += f"📝 **Queue Size:** {len(queue_list)}\n"
        
        # Show first few queue items
        if queue_list:
            message += "📋 **Queue Preview:**\n"
            for i, track in enumerate(queue_list[:3], 1):
                message += f"   {i}. {track.get('title', 'Unknown')}\n"
        
        await ctx.send(message)
        
    except Exception as e:
        await ctx.send(f"❌ Debug command failed: {str(e)}")

@bot.command(name='test', help='Test basic bot functionality')
async def test_command(ctx):
    await ctx.send("✅ Bot is responding! Commands are working.")

@bot.command(name='autorejoin', help='Toggle auto-rejoin when users join voice channel')
async def toggle_auto_join(ctx):
    global auto_join_enabled
    auto_join_enabled = not auto_join_enabled
    status = "enabled" if auto_join_enabled else "disabled"
    await ctx.send(f"Auto-rejoin {status}")

# Event handlers
@bot.event
async def on_ready():
    logging.info(f'{bot.user} is ready!')
    # Start status update task
    bot.loop.create_task(update_status_task())
    await check_and_join_voice_channel()

async def check_and_join_voice_channel():
    global auto_join_enabled
    
    if not auto_join_enabled:
        return
        
    guild = discord.utils.get(bot.guilds, id=int(config["guild_id"]))
    if guild is None:
        logging.error('Server not found!')
        return

    channel = discord.utils.get(guild.voice_channels, id=int(config["voice_channel_id"]))
    if channel is None:
        logging.error('Voice channel not found!')
        return

    if len(channel.members) > 0:
        await handle_user_joined(channel)

@bot.event
async def on_voice_state_update(member, before, after):
    global auto_join_enabled
    
    if not auto_join_enabled:
        return
        
    guild = discord.utils.get(bot.guilds, id=int(config["guild_id"]))
    if guild is None:
        return

    channel = discord.utils.get(guild.voice_channels, id=int(config["voice_channel_id"]))
    if channel is None:
        return

    if after.channel == channel:
        await handle_user_joined(channel)
    elif before.channel == channel:
        await handle_user_left(channel)

async def handle_user_joined(channel):
    global default_volume, is_playing_stream, current_source
    
    non_bot_members = [member for member in channel.members if not member.bot]
    if len(non_bot_members) == 0:
        return

    # If the bot is not already in the channel, join it and start playing the stream
    if not any(voice_client.channel == channel for voice_client in bot.voice_clients):
        voice_client = await channel.connect()
        await asyncio.sleep(1)  # Small delay to ensure connection is stable
        
        # Start playing the default Icecast stream
        try:
            audio_source = FFmpegPCMAudio(config["icecast_url"], options="-loglevel error")
            audio_source = PCMVolumeTransformer(audio_source, volume=default_volume)
            voice_client.play(audio_source)
            is_playing_stream = True
            current_source = audio_source
        except Exception as e:
            logging.error(f"Error starting stream: {e}")

async def handle_user_left(channel):
    global is_playing_stream
    
    non_bot_members = [member for member in channel.members if not member.bot]
    if len(non_bot_members) > 0:
        return

    # If the bot is the only one left in the channel, disconnect
    for voice_client in bot.voice_clients:
        if voice_client.channel == channel:
            await voice_client.disconnect()
            is_playing_stream = False

if __name__ == "__main__":
    bot.run(config["discord_bot_key"])
