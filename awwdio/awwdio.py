import asyncio
import datetime

import json
import multiprocessing
import os
import pathlib
import pprint as pp
import random

import shlex
import subprocess
import sys
import time
from collections import Counter

from dataclasses import dataclass, field

import pendulum

import pytz

from fastapi import FastAPI, HTTPException
from loguru import logger
from scheduler.asyncio import Scheduler


multiprocessing.set_start_method("fork")

ET = pytz.timezone("US/Eastern")

os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "hide"
import pygame


def sayThingWhatNeedBeSaid(how_many, voice, what, speed, count):
    # Voices can be listed with: say -v\?
    # On older versions, the macOS `say` command randomly exits with an error of "Speaking failed: -128"
    # so if we get an error code, KEEP TRYING FOREVER instead of dropping messages.
    got = 1
    while got == 1:
        logger.info("[{} :: {} ({})] Speaking: {}", how_many, voice, count, what)

        extra = ""
        if count and count > 1:
            extra = f" (repeated {count})"

        try:
            # TODO: add TIME speaking event log (and if older than 15-30 seconds, don't speak). (also speak OLD TIME if older than 3 seconds)
            p = subprocess.Popen(
                shlex.split(f'say -v "{voice}" -r {speed} "{what}{extra}"')
            )

            got = p.wait()
        except:
            # if we CTRL-C out of here, just stop trying immediately (if it works)
            logger.error("Goodbye!")
            p.kill()
            p.wait()
            sys.exit(0)


def speakerRunner(q):
    """Function running as a Process() receiving the update queue messages and playing songs.

    This pattern is because speaking through `say` pauses the program for the duration of
    the speaking, which would block all web server requests if running on the main process.

    Now the web server can continue to receive requests while speech is running, but only
    one phrase can be spoken at once because this speech runner is just one single-threaded
    worker consuming a linear queue of events."""
    while True:
        try:
            # "msg" is a tuple of arguments, we then apply to the function
            msg: tuple[Any] = q.get()
        except KeyboardInterrupt:
            logger.warning("Goodbye!")
            return
        except:
            logger.exception("How did the queue break?")

        try:
            sayThingWhatNeedBeSaid(*msg)
        except (KeyboardInterrupt, SystemExit):
            logger.info("Goodbye!")
            sys.exit(0)
            return
        except:
            logger.exception("Speaking no speaking?")
        finally:
            # queue entries are always done even if they throw exceptions
            q.task_done()


@dataclass
class Awwdio:
    timevoices: list[pathlib.Path] = field(default_factory=list)
    audiolib: list[pathlib.Path] = field(default_factory=list)

    # just a counter for how many speech requests we've received in total
    how_many: int = 0

    # deduplicating/debounce mechanism to avoid close-in-time received repeats
    speaker: Counter = field(default_factory=Counter)

    # queue for the worker process to receive speech events
    q: multiprocessing.JoinableQueue = field(
        default_factory=multiprocessing.JoinableQueue
    )

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

    def speak(self, voice, what, speed=250, count=0) -> None:
        self.how_many += 1
        self.q.put((self.how_many, voice, what, speed, count))

    async def wakevoice(self) -> None:
        remove = []

        # we store speak requests in a dict/Counter to automatically de-duplicate
        # sayings if multiple of the same speak requests show up too quickly together.
        # logger.info("Waking to process: {}", self.speaker)
        for key, count in self.speaker.items():
            voice, say, speed = key
            # Note: this doesn't speak directly — it just enqueues into the background worker.
            self.speak(voice, say, speed, count=count)
            remove.append(key)

        # this is probably a race condtion, but it's fine for now?
        for key in remove:
            del self.speaker[key]

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
            voice: str = "Alex", say: str = "Hello There", speed: int = 250
        ):
            logger.info("Received: {}", (voice, say, speed))
            self.speaker[(voice, say, speed)] += 1
            # logger.info("Current queue: {}", self.speaker)

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
                self.speaker[(voice, say, speed)] += 1

            # Parse scheduled event feed then
            # Schedule our schedule of events
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

            # https://digon.io/hyd/project/scheduler/t/master/pages/examples/quick_start.html

            # every 300ms check the speak queue.
            # we don't run speaking events immediately when they are received because
            # sometimes an external system will send us 10 notifications for the same
            # thing all at once, so we want to add a small deduplication time buffer.
            # (this isn't _perfect_ because duplicate requests can arrive while a current
            #  request is being played (so: request -> (remove and speak) || (arrive and queue) -> (remove and speak)),
            #  but there isn't a great way around it unless we want the speaker to enqueue a start/end speaking
            #  timestamp to then go back and delete messages from its speaking interval, but then we have
            #  uncounted duplicates not being spoken for?)
            schedule.cyclic(datetime.timedelta(milliseconds=300), self.wakevoice)

            await serve(self.app, config)

        try:
            p = multiprocessing.Process(
                target=speakerRunner, args=(self.q,), daemon=True
            )
            p.start()

            asyncio.run(launch_hypercorn())
        except (KeyboardInterrupt, SystemExit):
            logger.warning("Goodbye...")
            p.terminate()
            p.join()
            return


def cmd():
    import fire

    fire.Fire(Awwdio)
