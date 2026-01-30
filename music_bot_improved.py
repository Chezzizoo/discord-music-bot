import discord
from discord.ext import commands
from discord import app_commands
import yt_dlp
import asyncio
from typing import Optional, Dict, List
import logging
import random
import os
from dotenv import load_dotenv
from datetime import datetime, timedelta
import json

# ============================================================================
# CONFIGURATION
# ============================================================================

load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')

CONFIG = {
    'max_queue_size': 500,
    'max_search_results': 10,
    'max_playlist_size': 100,
    'ydl_timeout': 30,
    'ffmpeg_timeout': 600,  # 10 minutes
    'skip_cooldown': 2,  # seconds
    'cleanup_delay': 5,  # seconds before leaving empty channel
    'max_retry_attempts': 3,
}

# ============================================================================
# LOGGING
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)-8s %(name)s %(message)s'
)
logger = logging.getLogger(__name__)

# ============================================================================
# INTENTS & BOT
# ============================================================================

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ============================================================================
# YDL OPTIONS
# ============================================================================

YDL_OPTIONS = {
    'format': 'bestaudio/best',
    'noplaylist': True,
    'default_search': 'ytsearch',
    'quiet': True,
    'no_warnings': True,
    'extract_flat': 'in_playlist',
    'socket_timeout': CONFIG['ydl_timeout'],
    'ignoreerrors': False,
    'no_color': True,
    'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'extractor_args': {
        'youtube': {
            'player_client': ['android', 'web'],
            'skip': ['dash', 'hls']
        }
    },
}

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn -sn'
}

AUDIO_FILTERS = {
    'bass': {'description': 'Bass boost', 'filter': 'bass=g=10'},
    'nightcore': {'description': 'Nightcore (fast + pitched)', 'filter': 'atempo=1.25,asetrate=44100*1.25'},
    'slowmo': {'description': 'Slow motion', 'filter': 'atempo=0.8'},
    'treble': {'description': 'Treble boost', 'filter': 'treble=g=10'},
    'normal': {'description': 'Normal (no filter)', 'filter': ''},
}

# ============================================================================
# DATA CLASSES
# ============================================================================

class SongInfo:
    """Data class for song information"""
    def __init__(self, data: dict):
        self.title = data.get('title', 'Unknown')
        self.url = data.get('url', '')
        self.duration = data.get('duration', 0)
        self.thumbnail = data.get('thumbnail', '')
        self.uploader = data.get('uploader', 'Unknown')
        self.webpage_url = data.get('webpage_url', '')
        self.is_live = data.get('is_live', False)
        self.platform = data.get('platform', 'Unknown')
        self.extracted_at = datetime.now()
        self.is_cached = data.get('is_cached', False)
        
    def formatted_duration(self) -> str:
        """Return formatted duration string"""
        if self.duration == 0 or self.is_live:
            return "🔴 LIVE"
        minutes, seconds = divmod(int(self.duration), 60)
        hours, minutes = divmod(minutes, 60)
        if hours > 0:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes}:{seconds:02d}"
    
    def is_url_expired(self, max_age_hours: int = 6) -> bool:
        """Check if cached URL is too old"""
        age = datetime.now() - self.extracted_at
        return age > timedelta(hours=max_age_hours)


class SearchResult:
    """Data class for search results"""
    def __init__(self, title: str, artist: str, duration: str, url: str, index: int):
        self.title = title
        self.artist = artist
        self.duration = duration
        self.url = url
        self.index = index


class CommandCooldown:
    """Simple cooldown tracker"""
    def __init__(self, cooldown_seconds: float = 1.0):
        self.cooldown = cooldown_seconds
        self.last_used = {}
    
    def is_on_cooldown(self, user_id: int) -> bool:
        """Check if user is on cooldown"""
        if user_id not in self.last_used:
            return False
        
        elapsed = datetime.now() - self.last_used[user_id]
        return elapsed.total_seconds() < self.cooldown
    
    def set_cooldown(self, user_id: int):
        """Set cooldown for user"""
        self.last_used[user_id] = datetime.now()


class GuildAudioState:
    """Manages audio state for a guild"""
    def __init__(self):
        self.queue: List[Dict] = []
        self.now_playing: Optional[SongInfo] = None
        self.is_paused = False
        self.volume = 0.5
        self.loop_single = False
        self.loop_queue = False
        self.autoplay = False
        self.current_source = None
        self.current_filter = 'normal'
        self.text_channel: Optional[discord.TextChannel] = None
        self.voice_client: Optional[discord.VoiceClient] = None
        self.playlists: Dict[str, List[Dict]] = {}
        self.history: List[Dict] = []
        self.lock = asyncio.Lock()  # Prevent race conditions
        self.retry_count = 0
        self.url_cache: Dict[str, SongInfo] = {}  # Cache extracted URLs
        self.created_at = datetime.now()
    
    async def add_to_history(self, song_info: SongInfo):
        """Add song to play history"""
        async with self.lock:
            self.history.append({
                'title': song_info.title,
                'uploader': song_info.uploader,
                'url': song_info.webpage_url,
                'played_at': datetime.now().isoformat(),
                'duration': song_info.duration,
            })
            # Keep last 50 songs
            if len(self.history) > 50:
                self.history = self.history[-50:]


guild_data: Dict[int, GuildAudioState] = {}
skip_cooldown = CommandCooldown(CONFIG['skip_cooldown'])


def get_guild_state(guild_id: int) -> GuildAudioState:
    """Get or create guild audio state"""
    if guild_id not in guild_data:
        guild_data[guild_id] = GuildAudioState()
    return guild_data[guild_id]


# ============================================================================
# CLEANUP FUNCTION
# ============================================================================

async def cleanup_guild_state(guild_id: int):
    """Clean up unused guild state after timeout"""
    await asyncio.sleep(3600)  # 1 hour
    
    if guild_id in guild_data:
        state = guild_data[guild_id]
        if not state.voice_client or not state.voice_client.is_connected():
            logger.info(f"Cleaning up guild state for guild {guild_id}")
            del guild_data[guild_id]


# ============================================================================
# CORE AUDIO FUNCTIONS
# ============================================================================

def detect_platform(url: str) -> str:
    """Detect music platform from URL"""
    url_lower = url.lower()
    
    if 'spotify' in url_lower:
        return 'Spotify'
    elif 'soundcloud' in url_lower:
        return 'SoundCloud'
    elif 'youtube' in url_lower or 'youtu.be' in url_lower:
        return 'YouTube'
    elif 'music.apple' in url_lower or 'itunes' in url_lower:
        return 'Apple Music'
    else:
        return 'YouTube'


async def extract_spotify_metadata(spotify_url: str) -> Optional[tuple]:
    """
    Extract song title and artist from Spotify URL
    Returns: (song_title, artist_name) or None
    """
    try:
        logger.info(f"Extracting Spotify metadata from: {spotify_url}")
        
        import re
        track_match = re.search(r'track/([a-zA-Z0-9]+)', spotify_url)
        if not track_match:
            logger.error("Invalid Spotify URL format")
            return None
        
        with yt_dlp.YoutubeDL({'quiet': True, 'no_warnings': True}) as ydl:
            try:
                info = ydl.extract_info(spotify_url, download=False, process=False)
                if info and 'title' in info:
                    title = info.get('title', '')
                    if ' - ' in title:
                        parts = title.split(' - ', 1)
                        artist = parts[0].strip()
                        song = parts[1].strip()
                        logger.info(f"✅ Extracted: {song} by {artist}")
                        return (song, artist)
                    else:
                        return (title, "Unknown")
            except Exception as e:
                logger.warning(f"Could not extract Spotify metadata: {e}")
                return None
        
        return None
        
    except Exception as e:
        logger.error(f"Error extracting Spotify metadata: {e}")
        return None


async def search_youtube(query: str, limit: int = 10) -> List[SearchResult]:
    """
    Search YouTube for songs and return results
    """
    try:
        logger.info(f"Searching YouTube for: {query}")
        
        with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
            info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
            
            results = []
            if info and 'entries' in info:
                for idx, video in enumerate(info['entries'][:limit], 1):
                    duration = video.get('duration', 0)
                    minutes, seconds = divmod(int(duration), 60)
                    duration_str = f"{minutes}:{seconds:02d}" if duration > 0 else "LIVE"
                    
                    result = SearchResult(
                        title=video.get('title', 'Unknown'),
                        artist=video.get('uploader', 'Unknown'),
                        duration=duration_str,
                        url=video.get('url', video.get('webpage_url', '')),
                        index=idx
                    )
                    results.append(result)
            
            logger.info(f"✅ Found {len(results)} results")
            return results
    
    except Exception as e:
        logger.error(f"Error searching YouTube: {e}")
        return []


async def get_song_info(url: str, state: Optional[GuildAudioState] = None) -> Optional[SongInfo]:
    """
    Extract song info from URL with caching support
    """
    try:
        logger.info(f"Getting song info from: {url[:80]}...")
        
        # Check cache first
        if state and url in state.url_cache:
            cached = state.url_cache[url]
            if not cached.is_url_expired():
                logger.info(f"✅ Using cached song info for: {cached.title}")
                cached.is_cached = True
                return cached
        
        platform = detect_platform(url)
        
        with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
            info = ydl.extract_info(url, download=False)
            
            if not info:
                logger.error(f"No info extracted from: {url}")
                return None
            
            audio_url = info.get('url', '')
            if not audio_url:
                logger.error(f"No audio URL in info")
                return None
            
            song_info = SongInfo({
                'title': info.get('title', 'Unknown'),
                'url': audio_url,
                'duration': info.get('duration', 0),
                'thumbnail': info.get('thumbnail', ''),
                'uploader': info.get('uploader', 'Unknown'),
                'webpage_url': info.get('webpage_url', url),
                'is_live': info.get('is_live', False),
                'platform': platform,
            })
            
            # Cache it
            if state:
                state.url_cache[url] = song_info
            
            logger.info(f"✅ Song info extracted: {song_info.title}")
            return song_info
            
    except Exception as e:
        logger.error(f"Error getting song info: {e}")
        return None


async def play_next(guild_id: int, voice_client: discord.VoiceClient, state: GuildAudioState):
    """
    Play next song with proper error handling and retry logic
    """
    try:
        async with state.lock:
            if state.loop_single and state.now_playing:
                song_info = state.now_playing
                logger.info(f"Looping single: {song_info.title}")
                state.retry_count = 0
            elif state.queue:
                song_dict = state.queue.pop(0)
                
                if state.loop_queue:
                    state.queue.append(song_dict)
                
                url = song_dict.get('url', '')
                song_info = await get_song_info(url, state)
                
                if not song_info:
                    state.retry_count += 1
                    if state.retry_count < CONFIG['max_retry_attempts']:
                        logger.warning(f"Retry {state.retry_count}/{CONFIG['max_retry_attempts']}")
                        await state.text_channel.send(f"⚠️ Song failed, retrying... ({state.retry_count}/{CONFIG['max_retry_attempts']})")
                        await play_next(guild_id, voice_client, state)
                        return
                    else:
                        logger.error(f"Max retries exceeded")
                        await state.text_channel.send("❌ Could not load song after retries, skipping...")
                        await play_next(guild_id, voice_client, state)
                        return
                
                state.retry_count = 0
                state.now_playing = song_info
            else:
                state.now_playing = None
                logger.info("Queue finished")
                if not state.autoplay:
                    await state.text_channel.send("✅ Queue finished!")
                return
        
        try:
            logger.info(f"Creating audio source for: {state.now_playing.title}")
            
            before_options = '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5'
            after_options = '-vn -sn'
            
            if state.current_filter != 'normal':
                filter_str = AUDIO_FILTERS.get(state.current_filter, {}).get('filter', '')
                if filter_str:
                    after_options += f" -af {filter_str}"
            
            base_source = discord.FFmpegPCMAudio(
                state.now_playing.url,
                before_options=before_options,
                options=after_options
            )
            
            source = discord.PCMVolumeTransformer(base_source, volume=state.volume)
            state.current_source = source
            
            def after_playing(error):
                if error:
                    logger.error(f"Playback error: {error}")
                else:
                    if state.now_playing:
                        logger.info(f"Finished playing: {state.now_playing.title}")
                
                asyncio.run_coroutine_threadsafe(
                    play_next(guild_id, voice_client, state),
                    bot.loop
                )

            
            voice_client.play(source, after=after_playing)
            logger.info(f"✅ Now playing: {state.now_playing.title}")
            
            # Add to history
            await state.add_to_history(state.now_playing)
            
            # Send now playing embed
            embed = discord.Embed(
                title="🎵 Now Playing",
                color=discord.Color.green(),
                url=state.now_playing.webpage_url
            )
            embed.add_field(name="Track", value=state.now_playing.title, inline=False)
            embed.add_field(name="Artist", value=state.now_playing.uploader, inline=True)
            embed.add_field(name="Duration", value=state.now_playing.formatted_duration(), inline=True)
            embed.add_field(name="Queue", value=f"{len(state.queue)} songs remaining", inline=True)
            embed.add_field(name="Volume", value=f"{int(state.volume * 100)}%", inline=True)
            embed.add_field(name="Platform", value=state.now_playing.platform, inline=True)
            
            if state.current_filter != 'normal':
                filter_desc = AUDIO_FILTERS[state.current_filter]['description']
                embed.add_field(name="Filter", value=filter_desc, inline=True)
            
            if state.now_playing.thumbnail:
                embed.set_thumbnail(url=state.now_playing.thumbnail)
            
            if state.now_playing.is_cached:
                embed.set_footer(text="💾 Using cached audio URL")
            
            await state.text_channel.send(embed=embed)
            
        except Exception as e:
            logger.error(f"Error creating audio source: {e}")
            await state.text_channel.send(f"❌ Error playing audio: {str(e)[:100]}")
            await asyncio.sleep(1)
            await play_next(guild_id, voice_client, state)
    
    except Exception as e:
        logger.error(f"Error in play_next: {e}")


# ============================================================================
# SELECTION VIEW
# ============================================================================

class SongSelectView(discord.ui.View):
    """View for song selection dropdown"""
    
    def __init__(self, results: List[SearchResult], guild_id: int, interaction: discord.Interaction):
        super().__init__()
        self.results = results
        self.guild_id = guild_id
        self.interaction = interaction
        self.selected_result: Optional[SearchResult] = None
        
        options = [
            discord.SelectOption(
                label=f"{result.index}. {result.title[:90]}",
                value=str(result.index - 1),
                description=f"{result.artist} • {result.duration}"[:100]
            )
            for result in results
        ]
        
        self.select_menu = discord.ui.Select(
            placeholder="Select a song...",
            min_values=1,
            max_values=1,
            options=options
        )
        self.select_menu.callback = self.select_callback
        self.add_item(self.select_menu)
    
    async def select_callback(self, interaction: discord.Interaction):
        """Handle song selection"""
        try:
            index = int(self.select_menu.values[0])
            self.selected_result = self.results[index]
            
            await interaction.response.defer()
            
            state = get_guild_state(self.guild_id)
            voice_client = state.voice_client
            
            # Check queue size limit
            if len(state.queue) >= CONFIG['max_queue_size']:
                await interaction.followup.send(f"❌ Queue is full! (Max: {CONFIG['max_queue_size']})")
                return
            
            song_info = await get_song_info(self.results[index].url, state)
            if not song_info:
                await interaction.followup.send("❌ Could not retrieve song information")
                return
            
            state.queue.append({
                'url': self.results[index].url,
                'title': song_info.title,
                'uploader': song_info.uploader,
                'duration': song_info.duration,
                'webpage_url': song_info.webpage_url,
                'platform': song_info.platform,
                'thumbnail': song_info.thumbnail,
            })
            
            if not voice_client.is_playing():
                await play_next(self.guild_id, voice_client, state)
            else:
                embed = discord.Embed(
                    title="📝 Added to Queue",
                    description=f"**{song_info.title}**",
                    color=discord.Color.blue(),
                    url=song_info.webpage_url
                )
                embed.add_field(name="Artist", value=song_info.uploader, inline=True)
                embed.add_field(name="Duration", value=song_info.formatted_duration(), inline=True)
                embed.add_field(name="Position", value=f"#{len(state.queue)}", inline=True)
                
                if song_info.thumbnail:
                    embed.set_thumbnail(url=song_info.thumbnail)
                
                await interaction.followup.send(embed=embed)
        
        except Exception as e:
            logger.error(f"Error in select callback: {e}")
            await interaction.followup.send(f"❌ Error: {str(e)[:100]}")


# ============================================================================
# BOT EVENTS
# ============================================================================

@bot.event
async def on_ready():
    """Bot initialization"""
    logger.info(f'✅ Bot logged in as {bot.user}')
    try:
        synced = await bot.tree.sync()
        logger.info(f'✅ Synced {len(synced)} slash command(s)')
    except Exception as e:
        logger.error(f"Error syncing commands: {e}")


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    """Handle voice state changes"""
    if member == bot.user:
        return
    
    guild_id = member.guild.id
    state = get_guild_state(guild_id)
    
    if state.voice_client and state.voice_client.channel:
        if len(state.voice_client.channel.members) == 1:
            logger.info(f"All users left voice channel")
            state.queue.clear()
            state.now_playing = None
            if state.voice_client.is_playing():
                state.voice_client.stop()
            await asyncio.sleep(CONFIG['cleanup_delay'])
            if state.voice_client and len(state.voice_client.channel.members) == 1:
                await state.voice_client.disconnect()


# ============================================================================
# SLASH COMMANDS - PLAYBACK
# ============================================================================

@bot.tree.command(name="play", description="Play a song with selection menu")
@app_commands.describe(query="Song name, artist, or URL")
async def play_command(interaction: discord.Interaction, query: str):
    """Play a song - shows selection menu"""
    await interaction.response.defer()
    
    try:
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.followup.send("❌ You must be in a voice channel!")
            return
        
        voice_channel = interaction.user.voice.channel
        guild_id = interaction.guild_id
        state = get_guild_state(guild_id)
        state.text_channel = interaction.channel
        
        if state.voice_client is None or not state.voice_client.is_connected():
            try:
                state.voice_client = await voice_channel.connect()
                logger.info(f"Connected to {voice_channel.name}")
            except Exception as e:
                logger.error(f"Failed to connect: {e}")
                await interaction.followup.send(f"❌ Failed to connect: {e}")
                return
        elif state.voice_client.channel != voice_channel:
            await state.voice_client.move_to(voice_channel)
        
        # Handle Spotify URLs
        if 'spotify' in query.lower():
            metadata = await extract_spotify_metadata(query)
            if metadata:
                title, artist = metadata
                query = f"{title} {artist}"
                logger.info(f"Converted Spotify to search: {query}")
            else:
                await interaction.followup.send("⚠️ Could not extract Spotify metadata. Try searching by song name instead.\n💡 Example: `/play never gonna give you up`")
                return

        
        results = await search_youtube(query, limit=CONFIG['max_search_results'])
        
        if not results:
            await interaction.followup.send(f"❌ No results found for: **{query}**")
            return
        
        embed = discord.Embed(
            title="🔍 Select a song",
            description=f"Found {len(results)} results",
            color=discord.Color.blurple()
        )
        
        for result in results:
            embed.add_field(
                name=f"{result.index}. {result.title[:70]}",
                value=f"👤 {result.artist[:40]} • ⏱️ {result.duration}",
                inline=False
            )
        
        view = SongSelectView(results, guild_id, interaction)
        await interaction.followup.send(embed=embed, view=view)
    
    except Exception as e:
        logger.error(f"Error in play command: {e}")
        await interaction.followup.send(f"❌ Error: {str(e)[:100]}")


@bot.tree.command(name="pause", description="Pause the current song")
async def pause_command(interaction: discord.Interaction):
    """Pause playback"""
    try:
        state = get_guild_state(interaction.guild_id)
        
        if not state.voice_client or not state.voice_client.is_playing():
            await interaction.response.send_message("❌ No song is currently playing!")
            return
        
        state.voice_client.pause()
        state.is_paused = True
        await interaction.response.send_message("⏸️ **Paused**")
    except Exception as e:
        logger.error(f"Error in pause command: {e}")
        await interaction.response.send_message(f"❌ Error: {str(e)}")


@bot.tree.command(name="resume", description="Resume the paused song")
async def resume_command(interaction: discord.Interaction):
    """Resume playback"""
    try:
        state = get_guild_state(interaction.guild_id)
        
        if not state.voice_client or not state.voice_client.is_paused():
            await interaction.response.send_message("❌ No paused song to resume!")
            return
        
        state.voice_client.resume()
        state.is_paused = False
        await interaction.response.send_message("▶️ **Resumed**")
    except Exception as e:
        logger.error(f"Error in resume command: {e}")
        await interaction.response.send_message(f"❌ Error: {str(e)}")


@bot.tree.command(name="skip", description="Skip to the next song")
async def skip_command(interaction: discord.Interaction):
    """Skip current song"""
    try:
        state = get_guild_state(interaction.guild_id)
        user_id = interaction.user.id
        
        # Check cooldown
        if skip_cooldown.is_on_cooldown(user_id):
            await interaction.response.send_message("⏳ Skip is on cooldown! Please wait.", ephemeral=True)
            return
        
        if not state.voice_client or not state.voice_client.is_playing():
            await interaction.response.send_message("❌ No song is currently playing!")
            return
        
        state.voice_client.stop()
        skip_cooldown.set_cooldown(user_id)
        await interaction.response.send_message("⏭️ **Skipped**")
    except Exception as e:
        logger.error(f"Error in skip command: {e}")
        await interaction.response.send_message(f"❌ Error: {str(e)}")


@bot.tree.command(name="stop", description="Stop music and clear queue")
async def stop_command(interaction: discord.Interaction):
    """Stop playback and clear queue"""
    try:
        state = get_guild_state(interaction.guild_id)
        
        if not state.voice_client:
            await interaction.response.send_message("❌ Bot is not in a voice channel!")
            return
        
        state.voice_client.stop()
        state.queue.clear()
        state.now_playing = None
        
        await interaction.response.send_message("⏹️ **Stopped and cleared queue**")
    except Exception as e:
        logger.error(f"Error in stop command: {e}")
        await interaction.response.send_message(f"❌ Error: {str(e)}")


# ============================================================================
# SLASH COMMANDS - QUEUE
# ============================================================================

@bot.tree.command(name="queue", description="Show the current queue")
@app_commands.describe(page="Page number (1, 2, 3...)")
async def queue_command(interaction: discord.Interaction, page: int = 1):
    """Display the queue with pagination"""
    try:
        state = get_guild_state(interaction.guild_id)
        
        if not state.now_playing and not state.queue:
            await interaction.response.send_message("📋 **Queue is empty!**")
            return
        
        items_per_page = 10
        total_pages = (len(state.queue) + items_per_page - 1) // items_per_page
        page = max(1, min(page, total_pages))
        
        embed = discord.Embed(
            title="🎵 Music Queue",
            color=discord.Color.blurple(),
            description=f"Page {page}/{total_pages}"
        )
        
        if state.now_playing:
            embed.add_field(
                name="▶️ Currently Playing",
                value=f"**{state.now_playing.title}**\n👤 {state.now_playing.uploader}\n⏱️ {state.now_playing.formatted_duration()}",
                inline=False
            )
        
        if state.queue:
            start_idx = (page - 1) * items_per_page
            end_idx = start_idx + items_per_page
            queue_slice = state.queue[start_idx:end_idx]
            
            queue_text = ""
            
            for i, song in enumerate(queue_slice, start=start_idx + 1):
                duration = song.get('duration', 0)
                minutes, seconds = divmod(int(duration), 60)
                duration_str = f"{minutes}:{seconds:02d}" if duration > 0 else "LIVE"
                queue_text += f"{i}. **{song['title']}** `[{duration_str}]`\n"
            
            embed.add_field(
                name=f"📑 Up Next ({len(state.queue)} songs)",
                value=queue_text if queue_text else "Empty",
                inline=False
            )
        
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        logger.error(f"Error in queue command: {e}")
        await interaction.response.send_message(f"❌ Error: {str(e)}")


@bot.tree.command(name="remove", description="Remove a song from queue")
@app_commands.describe(position="Position in queue (1, 2, 3...)")
async def remove_command(interaction: discord.Interaction, position: int):
    """Remove song from queue"""
    try:
        state = get_guild_state(interaction.guild_id)
        
        if position < 1 or position > len(state.queue):
            await interaction.response.send_message(f"❌ Position must be between 1 and {len(state.queue)}")
            return
        
        removed_song = state.queue.pop(position - 1)
        await interaction.response.send_message(f"❌ **Removed:** {removed_song['title']}")
    except Exception as e:
        logger.error(f"Error in remove command: {e}")
        await interaction.response.send_message(f"❌ Error: {str(e)}")


@bot.tree.command(name="clear", description="Clear the entire queue")
async def clear_command(interaction: discord.Interaction):
    """Clear queue"""
    try:
        state = get_guild_state(interaction.guild_id)
        state.queue.clear()
        await interaction.response.send_message("🗑️ **Queue cleared**")
    except Exception as e:
        logger.error(f"Error in clear command: {e}")
        await interaction.response.send_message(f"❌ Error: {str(e)}")


@bot.tree.command(name="shuffle", description="Shuffle the queue")
async def shuffle_command(interaction: discord.Interaction):
    """Shuffle queue"""
    try:
        state = get_guild_state(interaction.guild_id)
        
        if not state.queue:
            await interaction.response.send_message("❌ Queue is empty!")
            return
        
        random.shuffle(state.queue)
        await interaction.response.send_message("🔀 **Queue shuffled**")
    except Exception as e:
        logger.error(f"Error in shuffle command: {e}")
        await interaction.response.send_message(f"❌ Error: {str(e)}")


# ============================================================================
# SLASH COMMANDS - CONTROLS
# ============================================================================

@bot.tree.command(name="volume", description="Set bot volume (0-100)")
@app_commands.describe(level="Volume level (0-100)")
async def volume_command(interaction: discord.Interaction, level: int):
    """Set playback volume"""
    try:
        if not (0 <= level <= 100):
            await interaction.response.send_message("❌ Volume must be between 0 and 100!")
            return
        
        state = get_guild_state(interaction.guild_id)
        state.volume = level / 100.0
        
        if state.voice_client and state.voice_client.source:
            state.voice_client.source.volume = state.volume
        
        await interaction.response.send_message(f"🔊 **Volume set to {level}%**")
    except Exception as e:
        logger.error(f"Error in volume command: {e}")
        await interaction.response.send_message(f"❌ Error: {str(e)}")


@bot.tree.command(name="loop", description="Toggle loop modes")
@app_commands.describe(mode="off, song, or queue")
async def loop_command(interaction: discord.Interaction, mode: str):
    """Set loop mode"""
    try:
        state = get_guild_state(interaction.guild_id)
        mode = mode.lower()
        
        if mode == "off":
            state.loop_single = False
            state.loop_queue = False
            msg = "🔁 **Loop disabled**"
        elif mode == "song":
            state.loop_single = True
            state.loop_queue = False
            msg = "🔂 **Song loop enabled**"
        elif mode == "queue":
            state.loop_single = False
            state.loop_queue = True
            msg = "🔁 **Queue loop enabled**"
        else:
            await interaction.response.send_message("❌ Mode must be: off, song, or queue")
            return
        
        await interaction.response.send_message(msg)
    except Exception as e:
        logger.error(f"Error in loop command: {e}")
        await interaction.response.send_message(f"❌ Error: {str(e)}")


@bot.tree.command(name="filter", description="Apply audio filters")
@app_commands.describe(effect="bass, nightcore, slowmo, treble, normal")
async def filter_command(interaction: discord.Interaction, effect: str):
    """Apply audio filter"""
    try:
        effect = effect.lower()
        
        if effect not in AUDIO_FILTERS:
            filters = ", ".join(AUDIO_FILTERS.keys())
            await interaction.response.send_message(f"❌ Available filters: {filters}")
            return
        
        state = get_guild_state(interaction.guild_id)
        state.current_filter = effect
        
        if state.voice_client and state.voice_client.is_playing():
            state.voice_client.stop()
            await asyncio.sleep(0.5)
            await play_next(interaction.guild_id, state.voice_client, state)
        
        filter_desc = AUDIO_FILTERS[effect]['description']
        await interaction.response.send_message(f"🎚️ **Filter applied:** {filter_desc}")
    except Exception as e:
        logger.error(f"Error in filter command: {e}")
        await interaction.response.send_message(f"❌ Error: {str(e)}")


@bot.tree.command(name="leave", description="Disconnect bot from voice channel")
async def leave_command(interaction: discord.Interaction):
    """Leave voice channel"""
    try:
        state = get_guild_state(interaction.guild_id)
        
        if not state.voice_client:
            await interaction.response.send_message("❌ Bot is not in a voice channel!")
            return
        
        state.queue.clear()
        state.now_playing = None
        await state.voice_client.disconnect()
        await interaction.response.send_message("👋 **Disconnected**")
    except Exception as e:
        logger.error(f"Error in leave command: {e}")
        await interaction.response.send_message(f"❌ Error: {str(e)}")


# ============================================================================
# SLASH COMMANDS - INFORMATION
# ============================================================================

@bot.tree.command(name="nowplaying", description="Show currently playing song")
async def nowplaying_command(interaction: discord.Interaction):
    """Show current song with details"""
    try:
        state = get_guild_state(interaction.guild_id)
        
        if not state.now_playing:
            await interaction.response.send_message("❌ No song is currently playing!")
            return
        
        song = state.now_playing
        embed = discord.Embed(
            title="🎵 Now Playing",
            color=discord.Color.green(),
            url=song.webpage_url
        )
        embed.add_field(name="Track", value=song.title, inline=False)
        embed.add_field(name="Artist", value=song.uploader, inline=True)
        embed.add_field(name="Duration", value=song.formatted_duration(), inline=True)
        embed.add_field(name="Queue", value=f"{len(state.queue)} songs remaining", inline=True)
        embed.add_field(name="Volume", value=f"{int(state.volume * 100)}%", inline=True)
        
        if state.loop_single:
            embed.add_field(name="Loop", value="🔂 Song", inline=True)
        elif state.loop_queue:
            embed.add_field(name="Loop", value="🔁 Queue", inline=True)
        
        if song.thumbnail:
            embed.set_thumbnail(url=song.thumbnail)
        
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        logger.error(f"Error in nowplaying command: {e}")
        await interaction.response.send_message(f"❌ Error: {str(e)}")


@bot.tree.command(name="history", description="Show recently played songs")
@app_commands.describe(limit="Number of songs to show (default: 10)")
async def history_command(interaction: discord.Interaction, limit: int = 10):
    """Display play history"""
    try:
        state = get_guild_state(interaction.guild_id)
        
        if not state.history:
            await interaction.response.send_message("📜 **No history yet!**")
            return
        
        limit = min(limit, len(state.history))
        recent = list(reversed(state.history[-limit:]))
        
        embed = discord.Embed(
            title="📜 Recently Played",
            color=discord.Color.blurple(),
            description=f"Last {limit} songs"
        )
        
        history_text = ""
        for i, song in enumerate(recent, 1):
            history_text += f"{i}. **{song['title']}** by {song['uploader']}\n"
        
        embed.add_field(name="Songs", value=history_text, inline=False)
        
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        logger.error(f"Error in history command: {e}")
        await interaction.response.send_message(f"❌ Error: {str(e)}")


@bot.tree.command(name="help", description="Show all commands")
async def help_command(interaction: discord.Interaction):
    """Display help"""
    embed = discord.Embed(
        title="🎵 Music Bot Help",
        color=discord.Color.blurple()
    )
    
    embed.add_field(
        name="▶️ Playback",
        value="`/play <query>` - Play with selection menu\n`/pause` - Pause\n`/resume` - Resume\n`/skip` - Skip\n`/stop` - Stop",
        inline=False
    )
    
    embed.add_field(
        name="📑 Queue",
        value="`/queue [page]` - Show queue\n`/remove <pos>` - Remove song\n`/clear` - Clear queue\n`/shuffle` - Shuffle",
        inline=False
    )
    
    embed.add_field(
        name="🎚️ Controls",
        value="`/volume <0-100>` - Set volume\n`/loop <off/song/queue>` - Loop mode\n`/filter <effect>` - Apply filter\n`/leave` - Disconnect",
        inline=False
    )
    
    embed.add_field(
        name="ℹ️ Info",
        value="`/nowplaying` - Current song\n`/history [limit]` - Play history\n`/help` - This message",
        inline=False
    )
    
    await interaction.response.send_message(embed=embed)


# ============================================================================
# ERROR HANDLER
# ============================================================================

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Handle command errors"""
    logger.error(f"Command error: {error}")
    
    if not interaction.response.is_done():
        await interaction.response.send_message(f"❌ Error: {str(error)[:100]}", ephemeral=True)
    else:
        await interaction.followup.send(f"❌ Error: {str(error)[:100]}", ephemeral=True)


# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    if not TOKEN or TOKEN == "":
        raise ValueError("❌ Please set DISCORD_TOKEN in .env file!")
    
    try:
        logger.info("🤖 Starting Discord Music Bot...")
        bot.run(TOKEN)
    except discord.LoginFailure:
        logger.error("❌ Invalid Discord token.")
    except Exception as e:
        logger.error(f"❌ Failed to start bot: {e}")
