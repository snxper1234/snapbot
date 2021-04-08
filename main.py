import asyncio
import os
from datetime import datetime
import random
import requests
import json
import keepalive


import functools
import itertools
import math

import discord
import youtube_dl
from async_timeout import timeout
from discord.ext import commands
from io import BytesIO
import sys



youtube_dl.utils.bug_reports_message = lambda: ''


class VoiceError(Exception):
    pass


class YTDLError(Exception):
    pass


class YTDLSource(discord.PCMVolumeTransformer):
    YTDL_OPTIONS = {
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

    FFMPEG_OPTIONS = {
        'before_options':
        '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
        'options': '-vn',
    }

    ytdl = youtube_dl.YoutubeDL(YTDL_OPTIONS)

    def __init__(self,
                 ctx: commands.Context,
                 source: discord.FFmpegPCMAudio,
                 *,
                 data: dict,
                 volume: float = 0.5):
        super().__init__(source, volume)

        self.requester = ctx.author
        self.channel = ctx.channel
        self.data = data

        self.uploader = data.get('uploader')
        self.uploader_url = data.get('uploader_url')
        date = data.get('upload_date')
        self.upload_date = date[6:8] + '.' + date[4:6] + '.' + date[0:4]
        self.title = data.get('title')
        self.thumbnail = data.get('thumbnail')
        self.description = data.get('description')
        self.duration = self.parse_duration(int(data.get('duration')))
        self.tags = data.get('tags')
        self.url = data.get('webpage_url')
        self.views = data.get('view_count')
        self.likes = data.get('like_count')
        self.dislikes = data.get('dislike_count')
        self.stream_url = data.get('url')

    def __str__(self):
        return '**{0.title}** by **{0.uploader}**'.format(self)

    @classmethod
    async def create_source(cls,
                            ctx: commands.Context,
                            search: str,
                            *,
                            loop: asyncio.BaseEventLoop = None):
        loop = loop or asyncio.get_event_loop()

        partial = functools.partial(cls.ytdl.extract_info,
                                    search,
                                    download=False,
                                    process=False)
        data = await loop.run_in_executor(None, partial)

        if data is None:
            raise YTDLError(
                'Couldn\'t find anything that matches `{}`'.format(search))

        if 'entries' not in data:
            process_info = data
        else:
            process_info = None
            for entry in data['entries']:
                if entry:
                    process_info = entry
                    break

            if process_info is None:
                raise YTDLError(
                    'Couldn\'t find anything that matches `{}`'.format(search))

        webpage_url = process_info['webpage_url']
        partial = functools.partial(cls.ytdl.extract_info,
                                    webpage_url,
                                    download=False)
        processed_info = await loop.run_in_executor(None, partial)

        if processed_info is None:
            raise YTDLError('Couldn\'t fetch `{}`'.format(webpage_url))

        if 'entries' not in processed_info:
            info = processed_info
        else:
            info = None
            while info is None:
                try:
                    info = processed_info['entries'].pop(0)
                except IndexError:
                    raise YTDLError(
                        'Couldn\'t retrieve any matches for `{}`'.format(
                            webpage_url))

        return cls(ctx,
                   discord.FFmpegPCMAudio(info['url'], **cls.FFMPEG_OPTIONS),
                   data=info)

    @staticmethod
    def parse_duration(duration: int):
        minutes, seconds = divmod(duration, 60)
        hours, minutes = divmod(minutes, 60)
        days, hours = divmod(hours, 24)

        duration = []
        if days > 0:
            duration.append('{} days'.format(days))
        if hours > 0:
            duration.append('{} hours'.format(hours))
        if minutes > 0:
            duration.append('{} minutes'.format(minutes))
        if seconds > 0:
            duration.append('{} seconds'.format(seconds))

        return ', '.join(duration)


class Song:
    __slots__ = ('source', 'requester')

    def __init__(self, source: YTDLSource):
        self.source = source
        self.requester = source.requester

    def create_embed(self):
        embed = (discord.Embed(
            title='Now playing',
            description='```css\n{0.source.title}\n```'.format(self),
            color=discord.Color.blurple()).add_field(
                name='Duration', value=self.source.duration).add_field(
                    name='Requested by',
                    value=self.requester.mention).add_field(
                        name='Uploader',
                        value='[{0.source.uploader}]({0.source.uploader_url})'.
                        format(self)).add_field(
                            name='URL',
                            value='[Click]({0.source.url})'.format(self)).
                 set_thumbnail(url=self.source.thumbnail))

        return embed


class SongQueue(asyncio.Queue):
    def __getitem__(self, item):
        if isinstance(item, slice):
            return list(
                itertools.islice(self._queue, item.start, item.stop,
                                 item.step))
        else:
            return self._queue[item]

    def __iter__(self):
        return self._queue.__iter__()

    def __len__(self):
        return self.qsize()

    def clear(self):
        self._queue.clear()

    def shuffle(self):
        random.shuffle(self._queue)

    def remove(self, index: int):
        del self._queue[index]


class VoiceState:
    def __init__(self, bot: commands.Bot, ctx: commands.Context):
        self.bot = bot
        self._ctx = ctx

        self.current = None
        self.voice = None
        self.next = asyncio.Event()
        self.songs = SongQueue()

        self._loop = False
        self._volume = 0.5
        self.skip_votes = set()

        self.audio_player = bot.loop.create_task(self.audio_player_task())

    def __del__(self):
        self.audio_player.cancel()

    @property
    def loop(self):
        return self._loop

    @loop.setter
    def loop(self, value: bool):
        self._loop = value

    @property
    def volume(self):
        return self._volume

    @volume.setter
    def volume(self, value: float):
        self._volume = value

    @property
    def is_playing(self):
        return self.voice and self.current

    async def audio_player_task(self):
        while True:
            self.next.clear()

            if not self.loop:
                # Try to get the next song within 3 minutes.
                # If no song will be added to the queue in time,
                # the player will disconnect due to performance
                # reasons.
                try:
                    async with timeout(180):  # 3 minutes
                        self.current = await self.songs.get()
                except asyncio.TimeoutError:
                    self.bot.loop.create_task(self.stop())
                    return

            self.current.source.volume = self._volume
            self.voice.play(self.current.source, after=self.play_next_song)
            await self.current.source.channel.send(
                embed=self.current.create_embed())

            await self.next.wait()

    def play_next_song(self, error=None):
        if error:
            raise VoiceError(str(error))

        self.next.set()

    def skip(self):
        self.skip_votes.clear()

        if self.is_playing:
            self.voice.stop()

    async def stop(self):
        self.songs.clear()

        if self.voice:
            await self.voice.disconnect()
            self.voice = None


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.voice_states = {}

    def get_voice_state(self, ctx: commands.Context):
        state = self.voice_states.get(ctx.guild.id)
        if not state:
            state = VoiceState(self.bot, ctx)
            self.voice_states[ctx.guild.id] = state

        return state

    def cog_unload(self):
        for state in self.voice_states.values():
            self.bot.loop.create_task(state.stop())

    def cog_check(self, ctx: commands.Context):
        if not ctx.guild:
            raise commands.NoPrivateMessage(
                'This command can\'t be used in DM channels.')

        return True

    async def cog_before_invoke(self, ctx: commands.Context):
        ctx.voice_state = self.get_voice_state(ctx)

    async def cog_command_error(self, ctx: commands.Context,
                                error: commands.CommandError):
        await ctx.send('An error occurred: {}'.format(str(error)))

    @commands.command(name='join', invoke_without_subcommand=True)
    async def _join(self, ctx: commands.Context):
        """Joins a voice channel."""

        destination = ctx.author.voice.channel
        if ctx.voice_state.voice:
            await ctx.voice_state.voice.move_to(destination)
            return
        ctx.voice_state.voice = await destination.connect()
        await ctx.message.add_reaction('✅')

    @commands.command(name='summon')
    @commands.has_permissions(manage_guild=True)
    async def _summon(self,
                      ctx: commands.Context,
                      *,
                      channel: discord.VoiceChannel = None):
        """Summons the bot to a voice channel.
        If no channel was specified, it joins your channel.
        """

        if not channel and not ctx.author.voice:
            raise VoiceError(
                'You are neither connected to a voice channel nor specified a channel to join.'
            )

        destination = channel or ctx.author.voice.channel
        if ctx.voice_state.voice:
            await ctx.voice_state.voice.move_to(destination)
            return

        ctx.voice_state.voice = await destination.connect()
        await ctx.message.add_reaction('✅')

    @commands.command(name='leave', aliases=['disconnect'])
    async def _leave(self, ctx: commands.Context):
        """Clears the queue and leaves the voice channel."""

        if not ctx.voice_state.voice:
            return await ctx.send('Not connected to any voice channel.')

        await ctx.voice_state.stop()
        del self.voice_states[ctx.guild.id]
        await ctx.message.add_reaction('👋')

    @commands.command(name='volume')
    async def _volume(self, ctx: commands.Context, *, volume: int):
        """Sets the volume of the player."""

        if not ctx.voice_state.is_playing:
            return await ctx.send('Nothing being played at the moment.')

        if 0 > volume > 100:
            return await ctx.send('Volume must be between 0 and 100')

        ctx.voice_state.volume = volume / 100
        await ctx.send('Volume of the player set to {}%'.format(volume))
        await ctx.message.add_reaction('✅')

    @commands.command(name='now', aliases=['current', 'playing'])
    async def _now(self, ctx: commands.Context):
        """Displays the currently playing song."""

        await ctx.send(embed=ctx.voice_state.current.create_embed())

    @commands.command(name='pause')
    async def _pause(self, ctx: commands.Context):
        """Pauses the currently playing song."""

        if ctx.voice_state.is_playing and ctx.voice_state.voice.is_playing():
            ctx.voice_state.voice.pause()
            await ctx.message.add_reaction('⏯')

    @commands.command(name='resume')
    async def _resume(self, ctx: commands.Context):
        """Resumes a currently paused song."""

        if ctx.voice_state.is_playing and ctx.voice_state.voice.is_paused():
            ctx.voice_state.voice.resume()
            await ctx.message.add_reaction('⏯')

    @commands.command(name='stop')
    async def _stop(self, ctx: commands.Context):
        """Stops playing song and clears the queue."""

        ctx.voice_state.songs.clear()

        if ctx.voice_state.is_playing:
            ctx.voice_state.voice.stop()
            await ctx.message.add_reaction('⏹')

    @commands.command(name='skip')
    async def _skip(self, ctx: commands.Context):

        if not ctx.voice_state.is_playing:
            return await ctx.send('Not playing any music right now...')

        voter = ctx.message.author
        if voter == ctx.voice_state.current.requester:
            await ctx.message.add_reaction('⏭')
            ctx.voice_state.skip()

        elif voter.id not in ctx.voice_state.skip_votes:
            ctx.voice_state.skip_votes.add(voter.id)
            total_votes = len(ctx.voice_state.skip_votes)

            if total_votes >= 3:
                await ctx.message.add_reaction('⏭')
                ctx.voice_state.skip()
            else:
                await ctx.send('Skip vote added, currently at **{}/3**'.format(
                    total_votes))

        else:
            await ctx.send('You have already voted to skip this song.')

    @commands.command(name='queue')
    async def _queue(self, ctx: commands.Context, *, page: int = 1):

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send('Empty queue.')

        items_per_page = 10
        pages = math.ceil(len(ctx.voice_state.songs) / items_per_page)

        start = (page - 1) * items_per_page
        end = start + items_per_page

        queue = ''
        for i, song in enumerate(ctx.voice_state.songs[start:end],
                                 start=start):
            queue += '`{0}.` [**{1.source.title}**]({1.source.url})\n'.format(
                i + 1, song)

        embed = (discord.Embed(description='**{} tracks:**\n\n{}'.format(
            len(ctx.voice_state.songs), queue)).set_footer(
                text='Viewing page {}/{}'.format(page, pages)))
        await ctx.send(embed=embed)

    @commands.command(name='shuffle')
    async def _shuffle(self, ctx: commands.Context):
        """Shuffles the queue."""

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send('Empty queue.')

        ctx.voice_state.songs.shuffle()
        await ctx.message.add_reaction('✅')

    @commands.command(name='remove')
    async def _remove(self, ctx: commands.Context, index: int):
        """Removes a song from the queue at a given index."""

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send('Empty queue.')

        ctx.voice_state.songs.remove(index - 1)
        await ctx.message.add_reaction('✅')

    @commands.command(name='loop')
    async def _loop(self, ctx: commands.Context):
        """Loops the currently playing song.
        Invoke this command again to unloop the song.
        """

        if not ctx.voice_state.is_playing:
            return await ctx.send('Nothing being played at the moment.')

        # Inverse boolean value to loop and unloop.
        ctx.voice_state.loop = not ctx.voice_state.loop
        await ctx.message.add_reaction('✅')

    @commands.command(name='play')
    async def _play(self, ctx: commands.Context, *, search: str):

        if not ctx.voice_state.voice:
            await ctx.invoke(self._join)

        async with ctx.typing():
            try:
                source = await YTDLSource.create_source(ctx,
                                                        search,
                                                        loop=self.bot.loop)
            except YTDLError as e:
                await ctx.send(
                    'An error occurred while processing this request: {}'.
                    format(str(e)))
            else:
                song = Song(source)

                await ctx.voice_state.songs.put(song)
                await ctx.send('Enqueued {}'.format(str(source)))

    @_join.before_invoke
    @_play.before_invoke
    async def ensure_voice_state(self, ctx: commands.Context):
        if not ctx.author.voice or not ctx.author.voice.channel:
            raise commands.CommandError(
                'You are not connected to any voice channel.')

        if ctx.voice_client:
            if ctx.voice_client.channel != ctx.author.voice.channel:
                raise commands.CommandError(
                    'Bot is already in a voice channel.')


PREFIX = 'c?'

intents = discord.Intents().default()
intents.members = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

bot.snipes = {}


# WAIT FOR BOT TO BE READY
@bot.event
async def on_ready():
    await bot.change_presence(
        status=discord.Status.online,
        activity=discord.Game(
            'c?help | Playing Among Us 😎 | SHARD 1'  # why shard 2 lol, nvm imma set up a service so the bot doesnt turn off by itself. :D
        ))
    print('[START] Bot Started : ' + str(datetime.now()))
    print('Logged in as')    
    print(bot.user.name)    
    print(bot.user.id)    
    print('------')    


# WAIT FOR MESSAGE
@bot.event
async def on_message(message):
    if message.author == bot.user:
        return 0
    await bot.process_commands(message)

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        await ctx.send("This command doesn't exist please use a valid command!")
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send('Whoaaa chill out bro, This command is on cooldown you can use it in about 5 seconds 😎')

@bot.command(brief='dead OS')
@commands.cooldown(1, 5, commands.BucketType.user)
async def crash(ctx):
    header = await ctx.send(
        ":( \n Your Snappy Bot OS build ran into a problem and needs to restart. We're \n just collecting some error info, and then we'll restart for you."
    )
    completion = await ctx.send('0% complete')
    info = await ctx.send(
        "For more infomation on this issue and possible fixes, visit `https://snapbot.com/stopcode`"
    )
    supp = await ctx.send('If you call a support person, give them this info:')
    stopcode = await ctx.send(
        "Stop code: SYSTEM_SERVICE_EXCEPTION \n What failed: condrv.sys")

    await asyncio.sleep(1)
    amount = random.randint(3, 17)

    while True:
        await asyncio.sleep(1)
        amount = random.randint(amount, amount + 10)

        if amount >= 100:
            await completion.edit(content="100% complete")

            await asyncio.sleep(1)
            #Delete old messages
            await asyncio.sleep(1)
            await header.delete()
            await completion.delete()
            await info.delete()
            await supp.delete()
            await stopcode.edit(
                content=
                "<:snappybot:815137536372113449> \n \n \n \n <a:loading:808162154166353942>"
            )
            await stopcode.edit(
                content=
                "<:snappybot:815137536372113449> \n \n \n \n Welcome <a:loading:808162154166353942>"
            )
            break

        if amount <= 100:
            await completion.edit(content=f"{amount}% complete")


@bot.event
async def on_message_delete(message):
    if not bot.snipes.get(str(message.channel.id)):
        bot.snipes[str(message.channel.id)] = [message]
    else:
        bot.snipes[str(message.channel.id)] += [message]


@bot.command(brief='snipe a deleted message')
async def snipe(ctx):
    sniped = bot.snipes.get(str(ctx.channel.id))
    if not sniped:
        await ctx.send('Nothing to snipe. :\'(')
        return
    sniped = bot.snipes[str(ctx.channel.id)].pop()
    embed = discord.Embed(title='Sniped!',
                          description=sniped.content,
                          timestamp=datetime.now())
    embed.set_author(icon_url=sniped.author.avatar_url_as(format='png'),
                     name=sniped.author.name)
    await ctx.send(embed=embed)
    


async def shutdown_program(ctx):
    await ctx.bot.change_presence(status=discord.Status.idle,
                                  activity=discord.Game('Stopping...'))
    await asyncio.sleep(2)
    sys.exit(0)

@bot.command(brief='shutdown the bot')
async def shutdown(ctx):
    id = str(ctx.author.id)
    if id == '800824616792227880' or id == '700436934702399509' or id == '665783345149509699':
        await ctx.send('Shutting down the bot! <a:loading:808162154166353942>')
        await shutdown_program(ctx)
    else:
        await ctx.send(
            "You dont have sufficient permmisions to perform this action! <:blobno:802718055196917773>"
        )


async def restart_program(ctx):
    await ctx.send('Restarting the bot! <a:loading:808162154166353942>')
    await ctx.bot.change_presence(status=discord.Status.idle,
                                  activity=discord.Game('Restarting...'))
    await asyncio.sleep(2)
    python = sys.executable
    os.execl(python, python, *sys.argv)


@bot.command(brief='restart the bot')
async def restart(ctx):
    id = str(ctx.author.id)
    if id == '800824616792227880' or id == '700436934702399509' or id == '665783345149509699':
        await restart_program(ctx)
    else:
        await ctx.send(
            "You dont have sufficient permmisions to perform this action! <:blobno:802718055196917773>"
        )

@bot.command(brief='ticket')
@commands.cooldown(1, 50, commands.BucketType.user)
async def ticket(ctx):
    guild = ctx.guild
    ticketrole = discord.utils.get(guild.roles, name="Ticket Requested")

    if not ticketrole:
        ticketrole = await guild.create_role(name="Ticket Requested")
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        guild.me: discord.PermissionOverwrite(read_messages=True),
        ticketrole: discord.PermissionOverwrite(read_messages=True)
    }
    await guild.create_text_channel('ticket', overwrites=overwrites)
    await ctx.author.add_roles(ticketrole)
    await ctx.message.add_reaction('🎟️')

@bot.command(brief='stuff')
@commands.cooldown(1, 5, commands.BucketType.user)
async def dostuff(ctx):
    await ctx.send("Hi Im Snappy Bot a Fun and happy bot.")

@bot.command(brief='')
async def version(ctx):
    id = str(ctx.author.id)
    if id == '800824616792227880' or id == '700436934702399509':
        await ctx.send ("• **Version**: 3.0.0  • **Version Name**: Oreo  • **What the bot runs on**: Power Crystals \💎 • **Bot Management**: https://repl.it/@FlurriousFlame/KosherDarlingUnderstanding#main.py • **⏣ Cogs and Vital bot parts**: 10 • **Bot status**: Operational and ready to go")
    else:
        await ctx.send(
            "Im Sorry, but this command contains very private bot info therefore you dont have permission to perform it. <:blobno:802718055196917773>"
        )

@bot.command(brief='Bot latency')
async def ping(ctx):
    datetime.timestamp(datetime.now())
    msg = await ctx.send('Pinging')
    await msg.edit(
        content=f'Pong! \n Response time is {round(bot.latency*1000)}ms.')


@bot.command(help="Play with .rps [your choice]")
async def rps(ctx):
    rpsGame = ['rock', 'paper', 'scissors']
    await ctx.send("Rock, paper, or scissors? Choose wisely...")

    def check(msg):
        return msg.author == ctx.author and msg.channel == ctx.channel and msg.content.lower(
        ) in rpsGame

    user_choice = (await bot.wait_for('message', check=check)).content

    comp_choice = random.choice(rpsGame)
    if user_choice == 'rock':
        if comp_choice == 'rock':
            await ctx.send(
                f'Well, that was weird. We tied.\nYour choice: {user_choice}\nMy choice: {comp_choice}'
            )
        elif comp_choice == 'paper':
            await ctx.send(
                f'Nice try, but I won that time!!\nYour choice: {user_choice}\nMy choice: {comp_choice}'
            )
        elif comp_choice == 'scissors':
            await ctx.send(
                f"Aw, you beat me. GG's\nYour choice: {user_choice}\nMy choice: {comp_choice}"
            )

    elif user_choice == 'paper':
        if comp_choice == 'rock':
            await ctx.send(
                f'Noo I lost D: You must be good at this game!\nYour choice: {user_choice}\nMy choice: {comp_choice}'
            )
        elif comp_choice == 'paper':
            await ctx.send(
                f'Oh, cool. We just tied. I call a rematch!!\nYour choice: {user_choice}\nMy choice: {comp_choice}'
            )
        elif comp_choice == 'scissors':
            await ctx.send(
                f"I win this round. Better luck next time fellow member!\nYour choice: {user_choice}\nMy choice: {comp_choice}"
            )

    elif user_choice == 'scissors':
        if comp_choice == 'rock':
            await ctx.send(
                f'HAHA!! I JUST CRUSHED YOU!! I rock!!\nYour choice: {user_choice}\nMy choice: {comp_choice}'
            )
        elif comp_choice == 'paper':
            await ctx.send(
                f'Bruh you won again!\nYour choice: {user_choice}\nMy choice: {comp_choice}'
            )
        elif comp_choice == 'scissors':
            await ctx.send(
                f"Oh well, we tied.\nYour choice: {user_choice}\nMy choice: {comp_choice}"
            )

@bot.command(aliases=['8ball', 'ball'])
async def _8ball(ctx, *, question):
    responses = [
        "It is certain.", "It is decidedly so.", "Without a doubt.",
        "Yes - definitely.", "You may rely on it.", "As I see it, yes.",
        "Most likely.", "Outlook good.", "Yes.", "Signs point to yes.",
        "Reply hazy, try again.", "Ask again later.",
        "Better not tell you now.", "Cannot predict now.",
        "Concentrate and ask again.", "Don't count on it.", "My reply is no.",
        "My sources say no.", "Outlook not so good.", "Very doubtful."
    ]
    await ctx.send(f'Question: {question}\nAnswer: {random.choice(responses)}')


@bot.command(aliases=['av' ])
async def avatar(ctx, *, avamember: discord.Member = None):
    userAvatarUrl = avamember.avatar_url
    await ctx.send(userAvatarUrl)
    embed = discord.Embed(colour=discord.Colour.blue())

    embed.set_author(name=avamember.name, icon_url=avamember.avatar_url)
    embed.add_field(name='Avatar: ', value=(avamember))

    await ctx.channel.send(embed=embed)


@bot.command()
async def dm(ctx, member: discord.Member, *, content):
    channel = await member.create_dm()
    await channel.send(content)
    await ctx.message.delete()
    embed = discord.Embed(colour=discord.Colour.blue())

    embed.set_author(name=member.name, icon_url=member.avatar_url)
    embed.add_field(name='Dmed user: ', value=(member))

    await ctx.channel.send(embed=embed)

@bot.command(name='help')
async def _helpcommand(ctx):

    helpmenu = '''
Here are The available categories... ⬇️
*** My Prefix is c?***

**General**

**c?invite** | get invite link for Senpai Bot
**c?emojis** | view all server emojis
**c?serverinfo** | view server info

**Fun**

**c?httpcat** | get a random http.cat
**c?cat** | get a random cat image
**c?dog** | get a random dog image
**c?fox** | get a random fox image
**c?quote** ***(@user) (text...)*** | fake discord message :P
**c?snipe** | snipe a deleted message
**c?ping**  | see the bot latency
**c?crash** | Makes Senpai Bot OS crash
**c?dostuff** | Just Makes the bot do something
**c?rps** | Play rock paper scissors with the bot
**c?dm** ***(@user)*** | DM the user you mention
**c?avatar** ***(@user)*** | Show that members avatar
**c?8ball** ***(question)*** | 8ball command! for example : c?8ball am I a good bot?

**Music**

**c?play** ***(query/url...)*** | play song in vc you are in
**c?queue** ***?(page)*** | view queue (optional page)
**c?join** | join the vc you are in
**c?leave** | leave the vc you are in and clear queue
**c?summon** ***(channel...)*** | summon bot to channel (default is your vc channel)
**c?skip** | skip song (if you are requester) otherwise vote to skip
**c?now** | view now playing
**c?shuffle** | shuffle queue
**c?stop** | stop playing and clear queue
**c?pause** | pause current song
**c?resume** | resume current song
**c?remove** ***(index)*** | remove song from queue

**Utility**

**c?ticket** | Make a ticket for help in the server
**c?purge** ***(limit)*** | purge messages

**Moderation**

**c?kick** ***(@user) ?(reason)*** | kick user
**c?ban** ***(@user) ?(reason)*** | ban user
**c?mute** ***(@user)*** | mute user
**c?unmute** ***(@user)*** | unmute user
**c?slowmode** ***(delay)*** | set slowmode delay
**c?whois** ***(@user)*** | get whois query on user
**c?shutdown** | Turns off the bot (Only the bot owner can use this command)
**c?restart** | Turns off the bot (Only the bot owner can use this command)
    '''
    embed = discord.Embed(title='Snappy Bot Help',
                          description=helpmenu,
                          color=0x1FEFD2)
    await ctx.send(embed=embed)

# KICK COMMAND
@bot.command()
@commands.has_permissions(kick_members=True)
async def kick(ctx, member: discord.Member, *, reason=None):
    await member.kick(reason=reason)
    await ctx.send(f'User {member} has been kicked.')
    await member.send(f'You Have Been Kicked From {ctx.guild.name}\nReason : {reason}\nStaff Username : {ctx.author.name}#{ctx.author.discriminator}')

# BAN COMMAND
@bot.command()
@commands.has_permissions(ban_members=True)
async def ban(ctx, member: discord.Member, *, reason=None):
    await member.ban(reason=reason)
    await ctx.send(f'User {member} has been banned.')
    await member.send(f'You Have Been Banned From {ctx.guild.name}\nReason : {reason}\nStaff Username : {ctx.author.name}#{ctx.author.discriminator}')

#WARN COMMAND
@bot.command(description="Warns the specified user.")
@commands.has_permissions(manage_guild=True)
async def warn(ctx, member: discord.Member,*,reason=None):
    guild = ctx.guild
    embed=discord.Embed(title="User Warned!", description="**{0}** was warned by **{1}**!".format(member, ctx.message.author), color=0xEFCA1F)
    embed.add_field(name="reason:", value=reason, inline=False)
    await ctx.send(embed=embed)
    await member.send(f" You have been Warned in: **{guild.name}** for the reason: **{reason}**")


# MUTE COMMAND
@bot.command(description="Mutes the specified user.")
@commands.has_permissions(manage_channels=True)
async def mute(ctx, member: discord.Member, *, reason=None):
    guild = ctx.guild
    mutedRole = discord.utils.get(guild.roles, name="Muted")

    if not mutedRole:
        mutedRole = await guild.create_role(name="Muted")

        for channel in guild.channels:
            await channel.set_permissions(mutedRole, speak=False, send_messages=False)
    embed=discord.Embed(title="User Muted!", description="**{0}** was unmuted by **{1}**!".format(member, ctx.message.author), color=0xF62424)
    embed.add_field(name="reason:", value=reason, inline=False)
    await ctx.send(embed=embed)
    await member.add_roles(mutedRole, reason=reason)
    await member.send(f" You have been muted in: **{guild.name}** for the reason: **{reason}**")



# UNMUTE COMMAND
@bot.command(pass_context=True)
@commands.has_permissions(manage_channels=True)
async def unmute(ctx, member: discord.Member):
    role = discord.utils.get(member.guild.roles, name='Muted')
    await member.remove_roles(role)
    embed=discord.Embed(title="User Unmuted!", description="**{0}** was unmuted by **{1}**!".format(member, ctx.message.author), color=0x41DD41)
    await ctx.send(embed=embed)


# SLOWMODE COMMAND
@bot.command()
@commands.has_permissions(manage_guild=True)
async def slowmode(ctx, seconds: int):
    await ctx.channel.edit(slowmode_delay=seconds)
    await ctx.send(
        f"Set the slowmode delay in this channel to {seconds} seconds!")


# PURGE COMMAND
@bot.command()
@commands.has_permissions(manage_guild=True)
async def purge(ctx, limit: int):
    await ctx.channel.purge(limit=limit)
    purge = await ctx.send(f"Purged {limit} messages!")
    await purge.delete()


@bot.command()
async def httpcat(ctx):
    codes = [
        100, 101, 102, 200, 201, 202, 204, 206, 207, 300, 301, 302, 303, 304,
        305, 307, 400, 401, 402, 403, 404, 405, 406, 408, 409, 410, 411, 412,
        413, 414, 415, 416, 417, 418, 420, 421, 422, 423, 424, 425, 426, 429,
        431, 444, 450, 451, 499, 500, 501, 502, 503, 504, 506, 507, 508, 509,
        510, 511, 599
    ]
    await ctx.send(f'https://http.cat/{random.choice(codes)}')


@bot.command()
async def cat(ctx):
    r = requests.get('https://aws.random.cat/meow')
    data = json.loads(r.text)
    await ctx.send(data['file'])


@bot.command()
async def dog(ctx):
    r = requests.get('https://dog.ceo/api/breeds/image/random')
    data = json.loads(r.text)
    await ctx.send(data['message'])


@bot.command()
async def fox(ctx):
    r = requests.get('https://randomfox.ca/floof/')
    data = json.loads(r.text)
    await ctx.send(data['image'])


@bot.command()
async def invite(ctx):
    invitei = '''
[Invite (Admin)](https://discord.com/api/oauth2/authorize?client_id=815126232004821015&permissions=8&scope=bot)    
[Invite (General Perms)](https://discord.com/api/oauth2/authorize?client_id=815126232004821015&permissions=507374838&scope=bot)

**Senpai Bot Owner**

! SnXper#7777

**Bot Developer**

RaidTheWeb#5514
    '''
    embed = discord.Embed(title='Invite Snappy Bot',
                          description=invitei,
                          color=0x41aa2c)
    await ctx.send(embed=embed)


@bot.command()
async def quote(ctx, member: discord.Member, *, text):
    r = requests.post('https://shellbot-image-server.raidtheweb.repl.co/quote',
                      json={
                          'avatar': str(member.avatar_url_as(format='png')),
                          'text': text,
                          'username': member.name
                      }).content

    b = BytesIO()
    b.write(r)
    b.seek(0)
    file = discord.File(b, filename='totallylegit.png')
    await ctx.send(file=file)


@bot.command()
async def whois(ctx, member: discord.Member):
    embed = discord.Embed(color=0x41aa2c)
    roles = ''
    for role in member.roles:
        roles += f'<@&{str(role.id)}>, '

    join_date = f'{member.joined_at.day}/{member.joined_at.month}/{member.joined_at.year}'
    create_date = f'{member.created_at.day}/{member.created_at.month}/{member.created_at.year}'

    boost_date = ''
    if not member.premium_since:
        boost_date = 'Not Boosting'
    else:
        boost_date = f'{member.premium_since.day}/{member.premium_since.month}/{member.premium_since.year}'

    roles = roles[:-2]
    embed.set_author(name=member.name, icon_url=member.avatar_url)
    embed.add_field(name='Username', value=member.name, inline=True)
    embed.add_field(name='Nickname', value=member.display_name, inline=True)
    embed.add_field(name='Joined At', value=join_date, inline=True)
    embed.add_field(name='Created At', value=create_date, inline=True)
    embed.add_field(name='Boosting Since', value=boost_date, inline=True)
    embed.add_field(name='Role Count', value=len(member.roles), inline=True)
    embed.add_field(name='Roles', value=roles, inline=True)
    await ctx.send(embed=embed)


@bot.command()
async def emojis(ctx):
    total = ''

    for emoji in ctx.guild.emojis:
        total += f'{bot.get_emoji(emoji.id)}'

    await ctx.send(total)

@bot.command(aliases=['si'])
async def serverinfo(ctx):
    textchannels = []
    voicechannels = []
    for channel in ctx.guild.channels:
        if isinstance(channel, discord.TextChannel):
            textchannels += [0]
        if isinstance(channel, discord.VoiceChannel):
            voicechannels += [0]

    humans = []
    bots = []

    for member in ctx.guild.members:
        if member.bot:
            bots += [0]
        else:
            humans += [0]

    emojis = ''
    for emoji in ctx.guild.emojis:
        emojis += f'{bot.get_emoji(emoji.id)}'

    if len(ctx.guild.emojis) >= 13:
        emojis = 'Emojis in a quantity to large to display.'

    if emojis == '':
        emojis = 'Server does not have any emojis.'

    features = ''
    for feature in ctx.guild.features:
        features += f'{feature}, '

    features = features[:-2]

    try:
        owner = f'{ctx.guild.owner.name}#{ctx.guild.owner.discriminator}'
    except:
        owner = 'none'

    embed = discord.Embed(title='Server Info',
                          description=f'''
• **Name::** {ctx.guild.name}
• **ID::** {ctx.guild.id}
• **Owner::** {owner}
• **Region::** {str(ctx.guild.region).capitalize()}
• **Created At::** {ctx.guild.created_at.strftime("%a, %d %b %Y")}
• **Text Channels::** {len(textchannels)}
• **Voice Channels::** {len(voicechannels)}
• **Roles::** {len(ctx.guild.roles)}
• **Verification Level::** {ctx.guild.verification_level}
• **Server Boost Info::** Level: {ctx.guild.premium_tier}, Boosts: {ctx.guild.premium_subscription_count}
• **Humans::** {len(humans)}
• **Bots::** {len(bots)}
• **Features::** {features}

• **Emojis::**
> {emojis}
        ''')
    await ctx.send(embed=embed)


bot.add_cog(Music(bot))

keepalive.keep_alive()
bot.run(os.getenv('TOKEN'))
