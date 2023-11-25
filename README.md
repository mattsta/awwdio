# Awwdio

Awwdio provides a http interface to the mac `say` command so you can have other services speak to you.


## Usage

```bash
pip install poetry -U

[clone, jump into directory]

poetry install
poetry run awwdio - start

— or —
poetry run awwdio --timevoices '["timevoice-market.json"]' --audiolib '["audiolib.json"]' - start

— or adjust the API host and port —
poetry run awwdio --audiolib '["audiolib.json"]' - start --host 0.0.0.0 --port 9994
```

You can optionally provide multiple filenames of timed events and multiple filenames of audio libraries. A timevoice sample schedule is provided for US stock market timed events (roughly).

## API

The API is simple.

There are two endpoints:

- `/say?voice=X&say=Y&speed=Z`
  - `voice` is an installed macos voice (System Settings -> Spoken Content -> System voice -> Manage Voices...)
    - can also be viewed with command line `say -v\?`
  - `say` is the phrase to speak
  - `speed` is the speech rate
  - All those options have defaults, so you can just call `/say` and it will do something.

- `/play?sound=X`
  - `sound` is a sound key defined in one of your `audiolib` mappings.
  - if no `sound` is provided, a random sound is played. if a non-existing sound key is requested, nothing happens.

## Nice Features

### Speech Deduplication / Debouncing

One nice feature is the speech endpoint automatically deduplicates requests if they arrive too quickly.

Here, instead of saying "thanks for the memories" 7 times in a row, it says "thanks for the memories — repeated 7" once (or, if the timing is off, it may say "repeated 6" then repeat another full one, but it's close enough).

```
for i in {1..7}; do curl localhost:8000/say\?say=thanks-for-the-memories; done
```


### Random Sound Playing

If no specific sound is provided, a random sound is chosen and played:

```
curl localhost:8000/play
```

## TODO

- Add optional authentication so you could open your speak engine to public IPs without worrying about unwanted API calls
- Add ability for sounds to be URLs and fetched/cached on startup automatically (current demo sounds are from https://mixkit.co/free-sound-effects/)
- Add marketplace or contrib directory for sound libraries and timevoice libraries
- Improve timevoice parser to allow specific date-at-time events instead of just daily repeating events
- Add simple client library so other apps can just call something like `await awwdio.Client(host, port, authtoken).speak("my cabbages!")`
- Add more sound options instead of just using `pygame` to play sounds (look into adding [synthesizer](https://github.com/yuma-m/synthesizer) and [pychord](https://github.com/yuma-m/pychord))
- Add ability for timed sounds with [start, interval, stop] durations too (e.g. "starting at 2pm, sound a ding every 20 seconds, stop at 2:15pm")
