import asyncio
import logging
from typing import Dict, Any, Optional, Set, Tuple, Union, List
import discord
from io import BytesIO
from discord.ext import commands, tasks
from utils.UBCCourseInfo import scrape_archive_course_info, scrape_course_info
from utils.Converters import Course
from utils.JsonTools import read_json, write_json
from utils.Checks import ban_members_check
from utils.Paginator import Paginator

"""
The JSON schema of this file should be:
{
    "<year_level: str>": {
        "base_channel": <channel_id: int>,
        "current_courses": {
            "<course_str>": <thread_id: int>,
            ...
        }
    },
    ...
}
"""
THREADS_CONFIG_FILENAME: str = "thread_channel_mapping.json"
CURRENT_COURSES_KEY: str = "current_courses"
BASE_CHANNEL_KEY: str = "base_channel"

AUTO_ARCHIVE_DURATION: int = 1440

ICS_PARSING_PREFIX: str = "SUMMARY:"
MAX_COURSES_PER_ICS: int = 15


class CourseThreads(commands.Cog):
    """
    Cog for course threads. Note that this entire cog is built upon the idea that
    all threads will be immutable and persistent -- that is, we do not expect threads
    to be deleted; hence, the bot metadata is append-only.
    """

    def __init__(self, client: commands.Bot):
        self.client: commands.Bot = client
        self.course_mappings: Dict[str, Any] = read_json(THREADS_CONFIG_FILENAME)
        self.course_modification_lock = asyncio.Lock()
        self.thread_refresher_task.start()

    @commands.group(aliases=["ct"])
    @commands.guild_only()
    @commands.check(ban_members_check)
    async def course_threads(self, ctx: commands.Context):
        """
        Command group related to creating course threads.
        """
        if ctx.invoked_subcommand is None:
            raise commands.errors.BadArgument

    @course_threads.command(aliases=["register", "reg"])
    @commands.guild_only()
    @commands.check(ban_members_check)
    async def register_base_channel(
        self, ctx: commands.Context, year_level: str, channel: discord.TextChannel
    ):
        """
        Registers a base channel for courses. Note that registering a year is immutable;
        that is, once set and courses are created, it cannot be changed.

        **Example(s)**
          `[p]ct register 1 #some-channel` - registers #some-channel as the base thread for all 1xx level courses
        """
        async with self.course_modification_lock:
            try:
                int(year_level)
            except ValueError:
                return await ctx.reply(
                    f"`{year_level}` isn't a valid integer. This should map to the first digit of the course code."
                )
            if year_level in self.course_mappings and len(
                self.course_mappings[year_level][CURRENT_COURSES_KEY]
            ):
                return await ctx.reply(
                    "There are already courses mapped to this year level; changing this is destructive, thus is manual. Exiting."
                )
            await channel.set_permissions(
                ctx.guild.default_role,
                send_messages=False,
                create_public_threads=False,
                create_private_threads=False,
                send_messages_in_threads=True,
                manage_threads=False,
            )
            self.course_mappings[year_level] = {
                BASE_CHANNEL_KEY: channel.id,
                CURRENT_COURSES_KEY: {},
            }
            write_json(THREADS_CONFIG_FILENAME, self.course_mappings)
            return await ctx.reply(
                f"Done! Added {channel.mention} as the base for year level: `{year_level}`."
            )

    @course_threads.command(aliases=["new", "create"])
    @commands.guild_only()
    @commands.check(ban_members_check)
    async def create_new_thread(self, ctx: commands.Context, course: Course):
        """
        Creates a new thread for the given course. The course format should be in DEPT###.

        **Example(s)**
          `[p]ct create CPEN331` - creates a new thread for CPEN331
        """
        async with self.course_modification_lock:
            create_result: Tuple[
                Optional[str], Optional[discord.Thread]
            ] = await self._create_course_thread(course)
            return await ctx.reply(create_result[0])

    @course_threads.command(aliases=["delete", "del"])
    @commands.guild_only()
    @commands.check(ban_members_check)
    async def delete_thread(self, ctx: commands.Context, course: Course):
        """
        Locks the thread for a given course. Try not to use it as it is relatively destructive.

        **Example(s)**
          `[p]ct delete CPEN331` - removes the thread mapping for CPEN331 and locks the thread
        """
        async with self.course_modification_lock:
            pre_check: Tuple[str, bool] = self._does_course_exist(course)
            if not pre_check[1]:
                return await ctx.reply(pre_check[0])
            course_thread: discord.Thread = self.client.get_channel(
                self.course_mappings[course.year_level][CURRENT_COURSES_KEY][
                    str(course)
                ]
            )
            await course_thread.edit(locked=True, archived=True)
            del self.course_mappings[course.year_level][CURRENT_COURSES_KEY][
                str(course)
            ]
            write_json(THREADS_CONFIG_FILENAME, self.course_mappings)
            await ctx.send(
                f"Done! Locked {course_thread.mention} and removed the mapping."
            )

    def _does_course_exist(self, course: Course) -> Tuple[str, bool]:
        if (
            course.year_level in self.course_mappings
            and str(course)
            in self.course_mappings[course.year_level][CURRENT_COURSES_KEY]
        ):
            return ("", True)
        else:
            return (f"A thread for `{course}` doesn't exist.", False)

    async def _get_course_thread(self, course: Course) -> discord.Thread:
        thread_id: int = self.course_mappings[course.year_level][CURRENT_COURSES_KEY][
            str(course)
        ]
        thread: Optional[discord.Thread] = self.client.get_channel(thread_id)
        if thread is None:
            thread = await self.client.fetch_channel(thread_id)
        return thread

    async def _create_course_thread(
        self, course: Course
    ) -> Tuple[str, Optional[discord.Thread]]:
        if course.year_level not in self.course_mappings:
            return (
                f"Base channel for year level (`{course.year_level}`) doesn't exist. Initialize it with `register_base_channel`.",
                None,
            )

        if str(course) in self.course_mappings[course.year_level][CURRENT_COURSES_KEY]:
            return (f"Course `{course}` already exists.", None)

        base_channel_id: int = self.course_mappings[course.year_level][BASE_CHANNEL_KEY]
        base_channel: discord.TextChannel = self.client.get_channel(base_channel_id)
        base_message: discord.Message = await base_channel.send(
            f"Thread for `{course}`"
        )
        created_thread: discord.Thread = await base_message.create_thread(
            name=str(course)
        )
        self.course_mappings[course.year_level][CURRENT_COURSES_KEY][
            str(course)
        ] = created_thread.id
        write_json(THREADS_CONFIG_FILENAME, self.course_mappings)
        return (f"Done! Created thread here: {created_thread.mention}", created_thread)

    @commands.group(aliases=["c"])
    async def courses(self, ctx: commands.Context):
        """
        Command group related to joining, leaving, and listing course threads.

        Note that you could also manually join and leave a thread using Discord's UI.
        """
        if ctx.invoked_subcommand is None:
            raise commands.errors.BadArgument

    @courses.command(name="join")
    @commands.guild_only()
    async def join_course(self, ctx: commands.Context, course: Course):
        """
        Join a course thread, if it exists. Note that this is idempotent.

        Note that there isn't a space between the course and the code.

        **Example(s)**
          `[p]course join CPEN331` - joins the thread for CPEN331
        """
        pre_check: Tuple[str, bool] = self._does_course_exist(course)
        if not pre_check[1]:
            return await ctx.reply(pre_check[0])
        course_thread_id: int = self.course_mappings[course.year_level][
            CURRENT_COURSES_KEY
        ][str(course)]
        course_thread: discord.Thread = self.client.get_channel(course_thread_id)
        await course_thread.add_user(ctx.author)
        return await ctx.reply(
            f"Done! Added you to {course_thread.mention}. You may want to change your notification settings for the thread."
        )

    @courses.command(name="leave")
    @commands.guild_only()
    async def leave_course(
        self, ctx: commands.Context, course: Union[Course, None] = None
    ):
        """
        Leave a course thread, if you're in it. Note that this is idempotent.

        Note that there isn't a space between the course and the code.

        **Example(s)**
          `[p]course leave CPEN331` - leaves the thread for CPEN331
        """
        pre_check: Tuple[str, bool] = self._does_course_exist(course)
        if not pre_check[1]:
            return await ctx.reply(pre_check[0])
        course_thread_id: int = self.course_mappings[course.year_level][
            CURRENT_COURSES_KEY
        ][str(course)]
        course_thread: discord.Thread = self.client.get_channel(course_thread_id)
        await course_thread.remove_user(ctx.author)
        return await ctx.reply(
            f"Done! Removed you from {course_thread.mention}, whether you were in it or not."
        )

    @courses.command(name="list", aliases=["l"])
    @commands.guild_only()
    async def list_courses(self, ctx: commands.Context):
        """
        List all the courses that currently have a course thread.

        **Example(s)**
          `[p]course list` - lists all the courses that currently have a thread
        """
        course_listing: List[str] = []
        for year, year_metadata in self.course_mappings.items():
            course_listing.append(f"**Level `{year}xx`**")
            for course, channel_id in year_metadata[CURRENT_COURSES_KEY].items():
                channel: discord.Thread = self.client.get_channel(channel_id)
                if not channel:
                    course_listing.append(
                        f"  - `{course}`: {channel_id} (error getting thread)"
                    )
                else:
                    course_listing.append(f"  - `{course}`: {channel.mention}")
        if not course_listing:
            return await ctx.reply("No courses found.")
        await Paginator(
            title="Available Courses", entries=course_listing, entries_per_page=25
        ).paginate(ctx)

    @courses.command(name="search", aliases=["s"])
    @commands.guild_only()
    async def search_courses(self, ctx: commands.Context, query: str):
        """
        Searches the thread directory for a case-insensitive match.

        **Example(s)**
          `[p]course search CPEN` - returns all threads that have CPEN (case-insensitive) in its title
          `[p]course search 331` - returns all threads that have 331 in its title
        """
        search_results: List[str] = []
        for year_metadata in self.course_mappings.values():
            for course, channel_id in year_metadata[CURRENT_COURSES_KEY].items():
                if (
                    query.lower() in course.lower()
                    or query.lower() in course.replace(" ", "").lower()
                ):
                    channel: discord.Thread = self.client.get_channel(channel_id)
                    if not channel:
                        search_results.append(
                            f"- `{course}`: {channel_id} (error getting thread)"
                        )
                    else:
                        search_results.append(f"- `{course}`: {channel.mention}")
        if not search_results:
            return await ctx.reply(
                f"No courses found for `{query}`.",
                allowed_mentions=discord.AllowedMentions(
                    everyone=False, users=False, roles=False
                ),
            )
        await Paginator(
            title=f"Courses Matching: `{query}`", entries=search_results
        ).paginate(ctx)

    @courses.command(name="import", aliases=["i"])
    async def import_courses(self, ctx: commands.Context):
        """
        Imports courses from a user-provided `.ics` file. Max of 15 courses at a time.
        Note that you must leave your old courses manually.

        The parser iterates through the file to search for `SUMMARY:{DEPT} {CODE}` strings.

        **Example**
          `[p]course import` (with attachment) - imports courses from the attachment
        """
        status_message: discord.Message = await ctx.reply("Processing...")
        async with self.course_modification_lock:
            if len(ctx.message.attachments) != 1:
                return await status_message.edit(
                    f"Make sure your message has exactly **1** attachment. I found **{len(ctx.message.attachments)}**."
                )

            # Parse the .ics file and find all the raw course strings
            calendar_file: BytesIO = BytesIO()
            await ctx.message.attachments[0].save(calendar_file)

            found_courses_raw: Set[str] = set()
            for line in calendar_file.readlines():
                try:
                    line: str = line.decode("utf-8")
                    if line.startswith(ICS_PARSING_PREFIX):
                        # Example of summary line from .ics is SUMMARY:CPEN 491 001
                        # The section/tutorial number _should_ be at the end, but it shouldn't matter here
                        found_courses_raw.add(
                            # Do a little preprocessing in order to make the Course type conversion easier
                            "".join(
                                line.replace(ICS_PARSING_PREFIX, "").strip().split()[:2]
                            )
                        )
                except UnicodeDecodeError:
                    return await status_message.edit(
                        "Failed to parse the file. Is it an `.ics` or text file?"
                    )

            if len(found_courses_raw) > MAX_COURSES_PER_ICS:
                status_message_str: str = (
                    f"The number of courses found exceeds the **{MAX_COURSES_PER_ICS}** maximum allowed."
                    + "\n"
                    + f"Found {len(found_courses_raw)}: {', '.join([f'`{course}`' for course in found_courses_raw])}"
                )

                logging.info(status_message_str)

                return await status_message.edit(
                    status_message_str,
                    allowed_mentions=discord.AllowedMentions(
                        everyone=False, users=False, roles=False
                    ),
                )

            # Try to convert into Course objects, tracking the failed results
            unparsed_courses: Set[str] = set()
            parsed_courses: Set[Course] = set()
            for course in found_courses_raw:
                parse_result: Optional[Course] = Course.parse(course)
                if parse_result:
                    parsed_courses.add(parse_result)
                else:
                    unparsed_courses.add(course)

            # Track courses that either aren't valid UBC courses, or its thread creation failed
            invalid_courses: Set[Course] = set()
            confirmed_courses: Set[Course] = set()

            # Gather the courses that already have a thread
            for course in parsed_courses:
                if self._does_course_exist(course)[1]:
                    confirmed_courses.add(course)

            # For the remaining courses, check if it's a valid UBC course and attempt to create it
            for course in parsed_courses - confirmed_courses:
                course_from_ubc: Optional[Course] = await scrape_course_info(course)
                if course_from_ubc is None:
                    invalid_courses.add(course)
                else:
                    create_result: Tuple[
                        Optional[str], Optional[discord.Thread]
                    ] = await self._create_course_thread(course)
                    if create_result[1] is not None:
                        confirmed_courses.add(course)
                    else:
                        invalid_courses.add(course)

        status_message_str: str = (
            "**Here's what I found**\n"
            + (
                ""
                if not confirmed_courses
                else f"\nYou'll be added to the following course threads:\n"
                + ", ".join([f"`{course}`" for course in confirmed_courses])
            )
            + (
                ""
                if not invalid_courses
                else "\nThese courses could not be found in UBC's course schedule or its thread failed to create:\n"
                + ", ".join([f"`{course}`" for course in invalid_courses])
            )
            + (
                ""
                if not unparsed_courses
                else "\nThese courses could not be parsed:\n"
                + ", ".join([f"`{course}`" for course in unparsed_courses])
            )
            + (
                "\nNothing\n"
                if not confirmed_courses
                and not invalid_courses
                and not unparsed_courses
                else ""
            )
            + "\n*Does that look right?* If it doesn't, feel free to try the command again."
        )
        await status_message.edit(
            status_message_str,
            allowed_mentions=discord.AllowedMentions(
                everyone=False, users=False, roles=False
            ),
        )

        logging.info(status_message_str)

        for course in confirmed_courses:
            course_thread: discord.Thread = await self._get_course_thread(course)
            await course_thread.add_user(ctx.author)

        await ctx.reply(
            "Done processing! You are now free to delete your uploaded file (if you wish)."
        )

    @tasks.loop(seconds=1)
    async def thread_refresher_task(self):
        """
        Threads automatically archive after inactivity. We'll iterate over all the threads
        and unarchive the ones that are archived. This shouldn't be expensive since it doesn't
        make any API calls unless the thread is archived (which was pushed to us by the gateway).
        """
        try:
            if self.client.is_ready():
                thread_ids: List[int] = [
                    channel_id
                    for year_metadata in self.course_mappings.values()
                    for channel_id in year_metadata[CURRENT_COURSES_KEY].values()
                ]
                for thread_id in thread_ids:
                    thread: Union[discord.Thread, None] = self.client.get_channel(
                        thread_id
                    )
                    # If the thread was archived and purged from the bot's cache, the
                    # getter will return None and we'll have to make an API call
                    if thread is None:
                        try:
                            thread: discord.Thread = await self.client.fetch_channel(
                                thread_id
                            )
                        except discord.errors.NotFound:
                            # Thrown if the thread isn't found, which should only happen
                            # if the thread was manually deleted; clean this thread up

                            # NOTE: this should _rarely_ be called. It's a user error if
                            # we ever get to this catch, but we do this defensively.
                            # Also, since it shouldn't get called at all, it's extremely inefficient
                            target_year_level: Union[None, str] = None
                            target_course: Union[None, str] = None
                            for (
                                year_level,
                                year_metadata,
                            ) in self.course_mappings.items():
                                for course, course_thread_id in year_metadata[
                                    CURRENT_COURSES_KEY
                                ].items():
                                    if course_thread_id == thread_id:
                                        target_year_level = year_level
                                        target_course = course
                                        break
                                if target_year_level and target_course:
                                    break
                            del self.course_mappings[target_year_level][
                                CURRENT_COURSES_KEY
                            ][target_course]
                            write_json(
                                THREADS_CONFIG_FILENAME,
                                self.course_mappings,
                            )
                            continue

                    if thread.archived:
                        logging.info(
                            f"Unarchived thread with thread ID: {thread_id}, name: {thread.name}"
                        )
                        await thread.edit(
                            archived=False,
                            auto_archive_duration=AUTO_ARCHIVE_DURATION,
                        )
        except Exception as e:
            logging.error(f"Thread refresher error: {e}")


def setup(client: commands.Bot):
    client.add_cog(CourseThreads(client))
