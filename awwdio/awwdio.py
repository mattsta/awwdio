import asyncio
import datetime

import json
import os
import pathlib
import pprint as pp
import random

import shlex
import subprocess
import sys
import threading
import time

from dataclasses import dataclass, field

import pendulum

import pytz

from fastapi import FastAPI, HTTPException
from loguru import logger
from scheduler.asyncio import Scheduler
from sortedcontainers import SortedDict


os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "hide"
import pygame


@dataclass(slots=True, unsafe_hash=True, order=True)
class SpeakEvent:
    voice: str = "Alex"
    say: str = "HELLO HELLO"
    speed: int = 250

    # if provided, don't speak this event if it is older than this epoch timestamp,
    # but also mark this field as not part of the object value for deduplication.
    deadline: int | float | None = field(default=None, compare=False, hash=False)

    priority: int = 0

    sort_index: tuple[int, int] = field(
        default=0, compare=False, hash=False, repr=False
    )

    def __post_init__(self) -> None:
        self.sort_index = (self.priority, self.deadline)


@logger.catch
def sayThingWhatNeedBeSaid(voice, what, speed):
    # Voices can be listed with: say -v\?
    # On older versions, the macOS `say` command randomly exits with an error of "Speaking failed: -128"
    # so if we get an error code, KEEP TRYING FOREVER instead of dropping messages.
    # Though, we also get an error if you try to use an uninstalled voice, so this just loops until you install the voice...
    got = 1
    while got == 1:
        try:
            # TODO: add TIME speaking event log (and if older than 15-30 seconds, don't speak). (also speak OLD TIME if older than 3 seconds)
            p = subprocess.Popen(shlex.split(f'say -v "{voice}" -r {speed} "{what}"'))

            got = p.wait()
        except:
            # if we CTRL-C out of here, just stop trying immediately (if it works)
            logger.error("Goodbye!")
            p.kill()
            p.wait()


@logger.catch
def speakerRunner(speakers, notify):
    """Function running as a Process() receiving the update queue messages and playing songs.

    This pattern is because speaking through `say` pauses the program for the duration of
    the speaking, which would block all web server requests if running on the main process.

    Now the web server can continue to receive requests while speech is running, but only
    one phrase can be spoken at once because this speech runner is just one single-threaded
    worker consuming a linear queue of events."""

    ran = 0
    while True:
        while not speakers:
            # logger.info("Checking...")
            notify.wait()

        # order elements in the task queue by (priority, deadline) for processing in priority order
        idx = 0
        while speakers:
            # logger.info("Speaking: {}", speakers)
            idx += 1

            # remove next event based on our (prio, deadline) sort order...
            event, count = speakers.popitem(0)

            # yes, fetch the timestamp on EACH iteration because speaking can take multiple
            # seconds and cause time to slip between each check here.
            now = datetime.datetime.now().timestamp()

            # if this event's speaking deadline has expired, don't speak anything.
            if event.deadline and now > event.deadline:
                logger.info(
                    "[Ignoring] :: {}] Expired {:.2f} seconds: {}",
                    count,
                    now - event.deadline,
                    event,
                )
                continue

            say = event.say
            if count > 1:
                say += f" (repeated {count})"

            ran += 1
            logger.info(
                "[batch {} :: {} :: {} :: {} :: {}]: {}",
                idx,
                ran,
                count,
                event.priority,
                event.voice,
                say,
            )

            sayThingWhatNeedBeSaid(event.voice, say, event.speed)

        # reset the 'notify' wakeup flag to false so we sleep again the next time if there's
        # no new updates ready.
        notify.clear()


@dataclass
class Awwdio:
    timevoices: list[pathlib.Path] = field(default_factory=list)
    audiolib: list[pathlib.Path] = field(default_factory=list)

    # just a counter for how many speech requests we've received in total
    how_many: int = 0

    # deduplicating/debounce mechanism to avoid close-in-time received repeats
    speakers: SortedDict = field(default_factory=SortedDict)

    notify: threading.Event = field(default_factory=threading.Event)

    # TODO: still need to print "X in backlog" events
    # TODO: still need to fix print-remaining-buffer-on-exit for trapping thread exit better
    def __post_init__(self) -> None:
        pygame.mixer.init()

        self.timevoices = [pathlib.Path(tv) for tv in self.timevoices]
        self.audiolib = [pathlib.Path(al) for al in self.audiolib]

        self.sounds = {}
        for al in self.audiolib:
            self.sounds |= json.loads(al.read_bytes())

        logger.info("Using audiolib mappings:\n{}", pp.pformat(self.sounds))

        self.app = FastAPI()

    def play(self, what: str | None = None) -> None:
        # Note: currently, sound playing is NOT guarded by a lock and it returns immediately,
        #       so if you have multiple 'play' requests at once, they ALL PLAY AT ONCE.
        #       In the future, we should have an option for "play-with-lock" or just "play-best-effort"
        # This runs on the primary webserver process because this sound playing is non-blocking.

        # also, we are loading the sound each time with may not be efficient? It works for now.
        ps = pygame.mixer.Sound(file=self.sounds.get(what))
        ps.play()

    def speak(self, voice, say, speed, deadline=None, priority=-1):
        event = SpeakEvent(voice, say, speed, deadline=deadline, priority=priority)

        # this is basically mocking a Counter() (or defaultdict(int)) via SortedDict instead
        if event in self.speakers:
            self.speakers[event] += 1
        else:
            self.speakers[event] = 1

        self.notify.set()

    def start(self, host: str = "127.0.0.1", port: int = 8000) -> None:
        @self.app.get("/play")
        async def playit(sound: str | None = None):
            # if sound provided and *DOES NOT EXIST* then we can't do anything
            if sound and sound not in self.sounds:
                raise HTTPException(
                    404,
                    detail=dict(sound=sound, msg="Sound isn't in your sound library!"),
                )

            # else, if no sound provided at all, just pick a random one and continue
            if not sound:
                sound = random.choice(tuple(self.sounds.keys()))

            logger.info("[{}] Playing...", sound)
            self.play(sound)

        @self.app.get("/say")
        async def sayit(
            voice: str = "Alex",
            say: str = "Hello There",
            speed: int = 250,
            deadline: int | float | None = None,
            prio: int = 100,
        ):
            # logger.info("Received: {}", (voice, say, speed, deadline, priority))
            self.speak(voice, say, speed, deadline, prio)
            # logger.info("Current queue: {}", self.speakers)

        # instead of running an external process-worker webserver, run it all locally
        # because we WANT a single-process non-multi-worker webserver here.
        # (one of the downsides of fastapi is how it doesn't have a simple "Start Server" mode
        #  and we have to bring in all this extra 3rd party server launching stuff as hacks)
        from hypercorn.asyncio import serve
        from hypercorn.config import Config

        config = Config()
        config.bind = [f"{host}:{port}"]

        @logger.catch
        async def launch_hypercorn():
            async def speakToMe(voice, say, speed=250):
                """Add speech request to speaker deduplication/debounce system."""
                self.speak(voice, say, speed, priority=-100)

            # Parse scheduled event feed then
            # Schedule our schedule of events
            # https://digon.io/hyd/project/scheduler/t/master/pages/examples/quick_start.html
            schedule = Scheduler(tzinfo=datetime.timezone.utc)
            for timevoice in self.timevoices:
                for row in json.loads(timevoice.read_bytes()):
                    when = row["when"]
                    tz = row["tz"]
                    voice = row["voice"]
                    say = row["say"]
                    interval = row["interval"]

                    at = pendulum.parse(when, strict=False)
                    fortime = datetime.time(
                        at.hour, at.minute, at.second, tzinfo=pytz.timezone(tz)
                    )

                    match interval:
                        case "minutely":
                            schedule.minutely(fortime, speakToMe, args=(voice, say))
                        case "hourly":
                            schedule.hourly(fortime, speakToMe, args=(voice, say))
                        case "daily":
                            schedule.daily(
                                fortime,
                                speakToMe,
                                args=(voice, say),
                            )
                        case _:
                            logger.error(
                                "[NOT SCHEDULED :: {} :: {} :: {}] Unknown interval provided: {}",
                                interval,
                                at,
                                voice,
                                say,
                            )

            if self.timevoices:
                logger.info("SCHEDULED EVENTS: {}", schedule)

            await serve(self.app, config)

        try:
            p = threading.Thread(
                target=speakerRunner, args=(self.speakers, self.notify)
            )

            # without p.daemon, the Thread refuses to recognize KeyboardInterrupt properly and
            # then it requires a double CTRL-C to exit. Now it exits cleanly with one CTRL-C, but
            # it just exits abruptly now and nothing really catches it anymore. Look into fixing maybe.
            p.daemon = True
            p.start()

            asyncio.run(launch_hypercorn())
        except (KeyboardInterrupt, SystemExit):
            logger.warning("Goodbye...")
            logger.warning(
                "Remaining speak events unspoken: {}", pp.pformat(self.speakers)
            )
            p.terminate()
            p.join()
            return


def cmd():
    import fire

    fire.Fire(Awwdio)
