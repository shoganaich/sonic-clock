# How Sonic Clock works

A plain-language tour of `weatherclock/weatherclock.py`, the one file that is the
whole app. Function names are given in `code` so you can search for them.

## The big picture

The Sonic Pad runs Debian with no desktop, no windows and no mouse pointer. There is
just a screen and a touch panel, and Linux lets a program talk to both directly as
if they were files:

| "File" | What it is | What the app does with it |
|---|---|---|
| `/dev/fb0` | The **framebuffer**: the memory the screen shows | Writes pixels into it |
| `/dev/input/event2` | The **touch panel** | Reads finger positions from it |
| `/usr/bin/brightness` | Creality's backlight tool | Runs it to dim, brighten or switch off the screen |

So the app works like this:

1. Draw a picture of the screen in memory with **Pillow** (a Python image library).
2. Copy that picture into `/dev/fb0`, and it appears on the display.
3. Read touches from `/dev/input/event2` and turn them into taps and swipes.
4. React to them by drawing a new picture.

There is no UI toolkit: every screen is a picture the app paints itself, much like
drawing on a canvas.

## Drawing a screen

Every screen has a `render_...` function, for example `render` (the dashboard),
`render_hourly`, `render_sea` and `render_settings`. Each one:

1. Makes a blank 1024 × 600 image (`background()` paints the gradient).
2. Draws on it with Pillow: rounded cards (`card()`), text in the NanumSquareRound
   font, and icons. The fonts have no weather symbols, so `icon()` draws suns, clouds,
   rain and so on from circles, lines and polygons.
3. Returns the finished image.

While drawing, it also records where each button is (a rectangle and what to do when
it is tapped) in a list called `self.buttons`. That list is how taps are matched to
buttons later.

## Putting the picture on the screen

The `Framebuffer` class does this. `show()` converts the Pillow image to raw bytes in
the order the screen expects (blue, green, red, alpha for each pixel) and writes them
into `/dev/fb0`. About 2.4 MB per full screen, and it appears instantly.

The animated weather icon on the dashboard uses `show_region()`, which only rewrites
the small square around the icon, so animating costs very little.

## Reading touches

`touch_loop()` runs in the background and reads `/dev/input/event2` forever. The touch
panel sends a stream of tiny messages:

- "finger is at X" and "finger is at Y"
- "finger down" or "finger up"
- "that's one complete update" (a sync message)

The loop remembers where and when the finger went down. When it goes up, it decides
what the gesture was:

| Gesture | Rule |
|---|---|
| **Swipe** left or right | Moved at least 120 pixels sideways (much more sideways than up or down) in under 1.5 seconds |
| **Long press** | Held for 2 seconds or more |
| **Tap** | Anything else |

It then puts the result, such as `("tap", 512, 300)`, into a queue called `events`. A
queue is a thread-safe mailbox: one part of the program drops messages in, another
takes them out.

## The main loop

`App.run()` is the heart of the app. It goes round and round:

1. **Check the time and state.** Has a timer finished? Has nobody touched the screen
   for 3 minutes (then go back to the clock)? Is it night, or inside the screen-off
   schedule?
2. **Draw if something changed.** Clock screens are only redrawn when what they show
   changes, usually once a minute when the time changes.
3. **Set the backlight.** Day or night brightness, or off during the screen-off hours.
4. **Wait** for the next event from the queue, but not longer than needed. Usually
   that means until the next minute starts, sooner if an icon is animating or a timer
   is counting down.
5. **Handle the event.** `handle()` takes a tap's position, checks which button in
   `self.buttons` contains it, and runs that button's action. Swipes change the page,
   a long press opens Settings.

Then it starts again. When nothing happens, the app is just sleeping in step 4, which
is why it uses almost no CPU.

## Several things at once (threads)

`main()` starts a few **threads**: parts of the program that run side by side.

| Thread | Job |
|---|---|
| Main (`App.run`) | Draws screens and handles touches |
| `touch_loop` | Reads the touch panel and posts gestures to the queue |
| `weather_loop` | Fetches weather every 10 minutes (every minute after an error, every 10 seconds until the first success) |
| Phone page (optional) | A small web server on port 8080 for changing settings from a phone |

They share data carefully. The latest weather lives in `state` and is protected by a
`lock`, so the screen never reads half-updated data. When new weather arrives,
`weather_loop` drops a `"refresh"` message into the same `events` queue, which wakes
the main loop to redraw.

## Weather and warnings

`fetch_all()` downloads the forecast, air quality and sea data from Open-Meteo as JSON.
`official_alerts()` adds official warnings from MeteoAlarm (Europe) or the US National
Weather Service. If the internet is down, the app keeps showing the last good data and
quietly retries.

## Settings

Settings are kept in a dictionary called `SET` and saved to
`/opt/weatherclock/settings.json` by `save_settings()`. It writes to a temporary file
first and then swaps it in, so a power cut can't leave a half-written file.

## Updating itself

From Settings → Status & updates → Check for updates, the app asks GitHub for the newest release
(`latest_release()`). If it's newer than `VERSION`, `install_release()` downloads it and
checks the new code compiles. `check_release()` then starts a separate Python that loads
the new file and runs its `self_test()`, which draws a few screens in memory. Only if that
works does it keep a backup of the old file, swap the new one in and restart the service.

It also leaves a note, `update-pending.json`, beside the app. Every start counts itself in
that note (`guard_update()`); once the new version has run for 2 minutes, `confirm_update()`
deletes it. If the note shows 3 starts that never got that far, the new version keeps
crashing, so the app copies `weatherclock.py.bak` back and starts the old version again.

## Previews without the screen

`python3 weatherclock.py --preview-ui <folder>` runs `preview_ui()`, which calls the same
`render_...` functions but saves each picture as a PNG instead of sending it to the
screen. That's how the screenshots in the README are made.
