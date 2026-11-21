# Salty's Stream Mixer (Windows 10/11)

A Sonar-style mixer. The program becomes your PC's audio output, splits sound into channels
(Game, Chat, Media, Aux, Mic), and sends the result to the outputs you choose.

## First-time setup
1. Install **VB-Cable** from https://vb-audio.com/Cable/ and restart your PC.
   Each app channel needs its own cable: the free download gives 1, and the A+B / C+D packs add more
   (4 app channels = 4 cables).
2. Install Salty's Stream Mixer (Setup.exe from the latest release) or run Portable.exe.
3. On first launch it assigns a cable to each channel, sets **Personal Mix** to your normal speakers/headphones,
   and makes **Game** your Windows output.

## The screen
- **Master panel (left)**
  - **Personal Mix** - the device you listen on (e.g. headphones). Required.
  - **Stream Mix** - an optional second output (e.g. speakers or a stream device).
  - **Mic Input** - your microphone; it feeds the Mic channel.
  - **Windows output** - which channel everything on your PC plays through.
- **Channels** - each has two faders: 🎧 Personal and 📡 Stream.
  - The **dot** above a fader turns that channel on/off for that mix.
  - The **speaker button** below mutes it in that mix.
  - **Apps** lists the programs on that channel. Drag one to another channel to move it.
  - **⚙** renames, picks the virtual cable, or removes the channel.
- **+ Add channel** adds more (each needs a free virtual cable).

Use one output or two: leave Stream Mix empty for one, or pick a device for both.

## Windows default bar
If Windows isn't using the mixer as its default output, a bar appears across the top with
**Click to auto set defaults**. Click it and the app switches Windows to the default channel
(Game unless you change it under *Windows output*). The app also does this by itself at startup.

## If sound ever goes missing
Re-open the app (closing it normally restores your original Windows output), or choose your
speakers in Windows Settings > System > Sound.

## If the app crashes or misbehaves
Open `%APPDATA%\SaltysStreamMixer\` (paste that into File Explorer's address bar). Two files help:
`app.log` (what the app was doing) and `crash.log` (errors). Send both when reporting a problem.

## For developers
`pip install -r requirements.txt`, put `SoundVolumeView.exe` next to the script, then
`python saltys_stream_mixer.py`. Config + crash log: `%APPDATA%\SaltysStreamMixer\`.
